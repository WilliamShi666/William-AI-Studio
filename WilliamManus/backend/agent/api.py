from fastapi import APIRouter, HTTPException, Depends, Request, Body, File, UploadFile, Form, Query  # type: ignore
from fastapi.responses import StreamingResponse  # type: ignore
import asyncio
import json
import traceback
import base64
import hashlib
from datetime import datetime, timezone
import time
import uuid
from typing import Optional, List, Dict, Any
import unicodedata
import jwt  # type: ignore
from pydantic import BaseModel, ValidationError  # type: ignore
import tempfile
import os

# from agentpress.thread_manager import ThreadManager
from services.postgresql import DBConnection
from services import redis
from services import regular_async_pool
from services import regular_run_attempts
from services.workspace_artifacts import workspace_artifacts
from utils.simple_auth_middleware import (
    get_current_user_id_from_jwt,
    get_user_id_from_stream_auth,
    verify_thread_access,
)
from utils.logger import logger, structlog

# from services.billing import check_billing_status, can_use_model
from utils.config import config
from sandbox.sandbox import create_sandbox, delete_sandbox, get_or_start_sandbox
from run_agent_background import (
    run_agent_background,
    regular_async_pool_dispatch as regular_async_pool_dispatch_actor,
    delete_sandbox_background,
    _cleanup_redis_response_list,
    update_agent_run_status,
)
from regular_supervisor_background import (
    regular_supervisor_background as regular_supervisor_background_actor,
)
from services.shadow_clone_model_config import (
    get_shadow_clone_model_config as read_shadow_clone_model_config,
    normalize_shadow_clone_selected_model_name,
)
from services.run_capacity import (
    build_run_capacity_limit_detail,
    get_run_capacity_cost,
    get_safe_run_capacity_snapshot,
    is_server_concurrency_budget_enabled,
    release_run_capacity_lease,
    try_acquire_run_capacity_lease,
)
from agentscope_integration.shadow_clone.sandbox_lease import (
    get_run_sandbox_lease,
    update_run_sandbox_lease,
)
from agentscope_integration.shadow_clone.file_delivery_source import (
    build_run_file_delivery_source,
)
from agentscope_integration.shadow_clone.constants import resolve_shadow_clone_mode
from agentscope_integration.shadow_clone.state_machine import (
    force_terminal_state,
    get_state,
)
from agentscope_integration.shadow_clone_v2 import (
    event_log as shadow_clone_v2_event_log,
)
from agentscope_integration.shadow_clone_v2.projection import (
    project_shadow_clone_v2_public_state,
)
from agent.run_status_projection import (
    build_response_list_key,
    read_terminal_status_from_response_tail,
)
from agent import regular_run_admission
from agent.shadow_clone_routes import shadow_clone_router

REGULAR_ASYNC_POOL_DISPATCH_CHANNEL = "regular_async_pool:dispatch"
_PHASE2_PUBLIC_EVENT_INDEX_EPOCH_STRIDE = 1_000_000_000
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID = "__persistent_supervisor_wakeup__"


def _parse_agent_run_metadata(metadata: Any) -> dict[str, Any]:
    if isinstance(metadata, dict):
        return metadata
    if isinstance(metadata, str) and metadata.strip():
        try:
            parsed = json.loads(metadata)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _agent_run_uses_shadow_clone_v2_event_stream(
    agent_run_data: dict[str, Any],
) -> bool:
    metadata = _parse_agent_run_metadata(agent_run_data.get("metadata"))
    shadow_clone_mode = str(metadata.get("shadow_clone_mode") or "").strip().lower()
    runtime = str(metadata.get("shadow_clone_runtime") or "").strip().lower()
    return shadow_clone_mode == "v2" or runtime == "v2"


def _agent_run_has_shadow_clone_v2_final_output(
    agent_run_data: dict[str, Any],
) -> bool:
    metadata = _parse_agent_run_metadata(agent_run_data.get("metadata"))
    shadow_clone_mode = str(metadata.get("shadow_clone_mode") or "").strip().lower()
    runtime = str(metadata.get("shadow_clone_runtime") or "").strip().lower()
    return shadow_clone_mode in {"v2", "on", "auto"} or runtime == "v2"


def _v2_event_type_value(event: Any) -> str:
    raw = getattr(event, "type", None)
    return str(getattr(raw, "value", raw) or "").strip()


def _v2_event_sequence(event: Any) -> int:
    value = getattr(event, "sequence", None)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _v2_event_id(event: Any) -> str:
    return str(getattr(event, "id", "") or "")


def _order_v2_stream_events(events: list[Any]) -> list[Any]:
    return sorted(
        enumerate(events),
        key=lambda item: (_v2_event_sequence(item[1]), _v2_event_id(item[1]), item[0]),
    )


def _is_terminal_v2_event(event: Any) -> bool:
    return _v2_event_type_value(event) in {
        "run_completed",
        "run_failed",
        "team_deleted",
    }


def _is_run_started_v2_event(event: Any) -> bool:
    return _v2_event_type_value(event) == "run_started"


def _build_v2_projection_stream_payload(
    *,
    agent_run_id: str,
    events_prefix: list[Any],
    event: Any,
    parent_status: Any,
) -> dict[str, Any]:
    projection = project_shadow_clone_v2_public_state(
        agent_run_id=agent_run_id,
        events=events_prefix,
        parent_status=parent_status,
    ) or {
        "status": "pending",
        "mode": "v2",
        "team": {},
        "tasks": {},
        "agents": {},
        "messages": {},
        "model": {},
        "final_output": None,
    }
    return {
        "type": "shadow_clone_v2_projection",
        "event_index": _v2_event_sequence(event),
        "event_id": _v2_event_id(event),
        "event_cursor": _v2_event_id(event),
        "v2_event_type": _v2_event_type_value(event),
        "agent_run_id": agent_run_id,
        "projection": projection,
        "delta": {
            "event": {
                "type": _v2_event_type_value(event),
                "sequence": _v2_event_sequence(event),
                "id": _v2_event_id(event),
            }
        },
        "metadata": {
            "stream_source": "shadow_clone_v2_event_log",
            "event_cursor": _v2_event_id(event),
        },
    }


_SHADOW_CLONE_V2_USER_FACING_MAIN_AGENT_PHASE_REASONS = frozenset(
    {
        "shadow_clone_v2_main_agent_planning_output",
        "shadow_clone_v2_final_output",
    }
)


def _is_shadow_clone_v2_user_facing_main_agent_assistant_response(
    response: dict[str, Any],
) -> bool:
    if response.get("type") != "assistant":
        return False
    if response.get("is_llm_message") is not True:
        return False

    metadata = _extract_stream_metadata(response)
    if str(metadata.get("activity_owner") or "").strip() != "main_agent":
        return False
    if (
        str(metadata.get("phase_reason") or "").strip()
        not in _SHADOW_CLONE_V2_USER_FACING_MAIN_AGENT_PHASE_REASONS
    ):
        return False
    if str(metadata.get("stream_status") or "").strip() not in {
        "chunk",
        "complete",
        "reasoning_chunk",
    }:
        return False

    content = _extract_stream_content(response)
    assistant_text = str(content.get("content") or "").strip()
    return bool(assistant_text)


def _is_shadow_clone_v2_user_facing_subagent_activity_response(
    response: dict[str, Any],
) -> bool:
    if response.get("type") != "subagent_activity":
        return False

    source = str(response.get("source") or "").strip()
    if source and source != "shadow_clone_v2":
        return False

    subtask_id = str(response.get("subtask_id") or "").strip()
    if not subtask_id:
        return False

    message_type = str(response.get("message_type") or "assistant").strip()
    if message_type != "assistant":
        return False

    metadata = _extract_stream_metadata(response)
    stream_status = str(metadata.get("stream_status") or "").strip()
    if stream_status not in {"chunk", "complete", "reasoning_chunk"}:
        return False

    content = _extract_stream_content(response)
    assistant_text = str(
        content.get("content") or content.get("reasoning_content") or ""
    ).strip()
    return bool(assistant_text)


def _is_shadow_clone_v2_user_facing_stream_response(
    response: dict[str, Any],
) -> bool:
    return (
        _is_shadow_clone_v2_user_facing_main_agent_assistant_response(response)
        or _is_shadow_clone_v2_user_facing_subagent_activity_response(response)
    )


def _shadow_clone_v2_assistant_response_identity(
    response: dict[str, Any],
) -> str:
    for field_name in ("message_id", "id"):
        value = str(response.get(field_name) or "").strip()
        if value:
            return f"{field_name}:{value}"
    return _json_dumps_stable(
        {
            "type": response.get("type"),
            "thread_id": response.get("thread_id"),
            "content": response.get("content"),
            "metadata": response.get("metadata"),
        }
    )


async def _read_shadow_clone_v2_user_facing_assistant_stream_responses(
    agent_run_id: str,
) -> list[dict[str, Any]]:
    response_list_key = build_response_list_key(agent_run_id)
    try:
        raw_responses = await redis.lrange(response_list_key, 0, -1)
    except Exception as error:
        logger.warning(
            "Failed to read Shadow Clone V2 assistant stream responses "
            "(agent_run_id=%s): %s",
            agent_run_id,
            error,
        )
        return []

    assistant_responses: list[dict[str, Any]] = []
    for raw_response in raw_responses:
        try:
            if isinstance(raw_response, bytes):
                raw_response = raw_response.decode("utf-8")
            parsed_response = json.loads(raw_response)
        except Exception:
            continue
        if not isinstance(parsed_response, dict):
            continue
        if not _is_shadow_clone_v2_user_facing_stream_response(parsed_response):
            continue

        response = dict(parsed_response)
        # V2 projections use event_index/event_cursor as the durable V2 event cursor.
        # User-facing assistant messages from the Redis response list are regular
        # chat messages, so exposing the list index as event_index would make the
        # frontend cursor drop later V2 projection events with lower sequence values.
        response.pop("event_index", None)
        response.pop("event_cursor", None)
        assistant_responses.append(response)

    return assistant_responses


def _count_by_field(rows: list[dict[str, Any]], field_name: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        value = row.get(field_name) or "unknown"
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _safe_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _json_dumps_stable(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _normalize_event_timestamp(event: dict[str, Any]) -> Any:
    return event.get("timestamp") or event.get("created_at") or event.get("createdAt")


def _extract_event_text(content: Any) -> str:
    parsed_content = content
    if isinstance(content, str):
        try:
            parsed_content = json.loads(content)
        except Exception:
            return content.strip()

    if isinstance(parsed_content, list):
        text_parts = [
            _extract_event_text(part)
            for part in parsed_content
            if _extract_event_text(part)
        ]
        return (
            " ".join(text_parts).strip()
            if text_parts
            else _json_dumps_stable(parsed_content)
        )

    if isinstance(parsed_content, dict) and "parts" in parsed_content:
        return _extract_event_text(parsed_content["parts"])
    if isinstance(parsed_content, dict):
        text_value = parsed_content.get("text")
        if isinstance(text_value, str):
            return text_value.strip()
        content_value = parsed_content.get("content")
        if isinstance(content_value, str):
            return content_value.strip()
        if content_value is not None:
            return _extract_event_text(content_value)
        return _json_dumps_stable(parsed_content)

    return str(parsed_content).strip() if parsed_content is not None else ""


def _normalize_message_content(content: Any) -> str:
    parsed_content = content
    if isinstance(content, str):
        try:
            parsed_content = json.loads(content)
        except Exception:
            return content.strip()
    return _extract_event_text(parsed_content)


def _normalize_message_timestamp(message: dict[str, Any]) -> str:
    return _normalize_timestamp_value(
        message.get("created_at")
        or message.get("timestamp")
        or message.get("createdAt")
        or ""
    )


def _normalize_timestamp_value(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")


def _convert_assistant_events_to_messages(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    assistant_messages = []
    for event in events:
        try:
            assistant_content = {
                "role": "assistant",
                "content": _extract_event_text(event.get("content")),
            }
            assistant_messages.append(
                {
                    "message_id": str(event.get("id")) if event.get("id") else None,
                    "thread_id": (
                        str(event.get("session_id"))
                        if event.get("session_id")
                        else None
                    ),
                    "type": "assistant",
                    "role": "assistant",
                    "is_llm_message": True,
                    "content": json.dumps(assistant_content, ensure_ascii=False),
                    "metadata": json.dumps(
                        {
                            "source": "events_fallback",
                            "event_id": (
                                str(event.get("id")) if event.get("id") else None
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    "created_at": _normalize_event_timestamp(event),
                    "updated_at": _normalize_event_timestamp(event),
                    "agent_id": None,
                    "agent_version_id": None,
                }
            )
        except Exception as e:
            logger.warning(
                f"skip format error assistant event {event.get('id', 'unknown')}: {e}"
            )
    return assistant_messages


def _message_event_ids(messages: list[dict[str, Any]]) -> frozenset[str]:
    event_ids = []
    for message in messages:
        metadata = _safe_json_object(message.get("metadata"))
        candidate_ids = (
            metadata.get("event_id"),
            metadata.get("source_event_id"),
            metadata.get("adk_event_id"),
        )
        event_ids.extend(str(event_id) for event_id in candidate_ids if event_id)
    return frozenset(event_ids)


def _assistant_timestamp_content_keys(
    messages: list[dict[str, Any]],
) -> frozenset[tuple[str, str]]:
    return frozenset(
        (
            _normalize_message_timestamp(message),
            _normalize_message_content(message.get("content")),
        )
        for message in messages
        if message.get("type") == "assistant"
        and _normalize_message_timestamp(message)
        and _normalize_message_content(message.get("content"))
    )


def _assistant_run_content_keys(
    messages: list[dict[str, Any]],
) -> frozenset[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for message in messages:
        if message.get("type") != "assistant":
            continue
        metadata = _safe_json_object(message.get("metadata"))
        agent_run_id = str(metadata.get("agent_run_id") or "").strip()
        content = _normalize_message_content(message.get("content"))
        if agent_run_id and content:
            keys.add((agent_run_id, content))
    return frozenset(keys)


def _dedupe_event_fallback_messages(
    *,
    table_messages: list[dict[str, Any]],
    fallback_messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    table_event_ids = _message_event_ids(table_messages)
    table_message_ids = frozenset(
        str(message.get("message_id"))
        for message in table_messages
        if message.get("message_id")
    )
    table_assistant_keys = _assistant_timestamp_content_keys(table_messages)
    table_assistant_run_content_keys = _assistant_run_content_keys(table_messages)
    return [
        message
        for message in fallback_messages
        if message.get("message_id")
        and str(message["message_id"]) not in table_event_ids
        and str(message["message_id"]) not in table_message_ids
        and (
            _normalize_message_timestamp(message),
            _normalize_message_content(message.get("content")),
        )
        not in table_assistant_keys
        and (
            str(_safe_json_object(message.get("metadata")).get("agent_run_id") or "").strip(),
            _normalize_message_content(message.get("content")),
        )
        not in table_assistant_run_content_keys
    ]


def _latest_assistant_info(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    assistant_messages = [
        message for message in messages if message.get("type") == "assistant"
    ]
    if not assistant_messages:
        return None
    latest = max(
        assistant_messages,
        key=lambda message: _normalize_timestamp_value(message.get("created_at")),
    )
    return {
        "message_id": latest.get("message_id"),
        "created_at": latest.get("created_at"),
        "source": _safe_json_object(latest.get("metadata")).get("source", "messages"),
    }


async def _convert_shadow_clone_v2_final_outputs_to_messages(
    client: Any,
    thread_id: str,
) -> list[dict[str, Any]]:
    try:
        runs_result = (
            await client.table("agent_runs")
            .select("agent_run_id, thread_id, status, metadata, created_at")
            .eq("thread_id", thread_id)
            .execute()
        )
    except Exception as error:
        logger.warning(
            "failed to inspect Shadow Clone V2 runs for thread message fallback: "
            "thread_id=%s error=%s",
            thread_id,
            error,
        )
        return []

    fallback_messages: list[dict[str, Any]] = []
    for run_row in list(getattr(runs_result, "data", None) or []):
        run = dict(run_row)
        agent_run_id = str(run.get("agent_run_id") or "").strip()
        if not agent_run_id or not _agent_run_has_shadow_clone_v2_final_output(run):
            continue
        try:
            events = await shadow_clone_v2_event_log.read_events(
                agent_run_id,
                start="-",
            )
        except Exception as error:
            logger.warning(
                "failed to read Shadow Clone V2 event log for message fallback: "
                "agent_run_id=%s error=%s",
                agent_run_id,
                error,
            )
            continue
        if not events:
            continue
        projection = project_shadow_clone_v2_public_state(
            agent_run_id=agent_run_id,
            events=events,
            parent_status=run.get("status"),
        )
        final_output = (projection or {}).get("final_output")
        final_content = (
            final_output.get("content")
            if isinstance(final_output, dict)
            else final_output
        )
        final_content = str(final_content or "").strip()
        if not final_content:
            continue
        created_at_value = (
            final_output.get("created_at")
            if isinstance(final_output, dict)
            else None
        ) or (projection or {}).get("updated_at") or run.get("created_at")
        if hasattr(created_at_value, "isoformat"):
            created_at = created_at_value.isoformat()
        else:
            created_at = str(created_at_value or "")
        metadata = {
            "source": "shadow_clone_v2_event_log_final_output",
            "agent_run_id": agent_run_id,
            "shadow_clone_mode": "v2",
            "stream_status": "complete",
        }
        fallback_messages.append(
            {
                "message_id": f"shadow-clone-v2-final-{agent_run_id}",
                "thread_id": thread_id,
                "type": "assistant",
                "role": "assistant",
                "is_llm_message": True,
                "content": json.dumps(
                    {"role": "assistant", "content": final_content},
                    ensure_ascii=False,
                ),
                "metadata": json.dumps(metadata, ensure_ascii=False),
                "created_at": created_at,
                "updated_at": created_at,
                "agent_id": None,
                "agent_version_id": None,
            }
        )
    return fallback_messages


async def _fetch_rows_batched(
    query_factory: Any,
    *,
    select_fields: str,
    filters: tuple[tuple[str, Any], ...],
    order_field: str | None = None,
    batch_size: int = 1000,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        query = query_factory().select(select_fields)
        for key, value in filters:
            query = query.eq(key, value)
        if order_field:
            query = query.order(order_field, desc=False)
        query = query.range(offset, offset + batch_size - 1)
        result = await query.execute()
        batch = result.data or []
        rows = [*rows, *batch]
        if len(batch) < batch_size:
            return rows
        offset += batch_size


async def _fetch_thread_visible_message_count(client: Any, thread_id: str) -> int:
    messages = await _fetch_rows_batched(
        lambda: client.table("messages"),
        select_fields="message_id,metadata,type,content,created_at",
        filters=(("thread_id", thread_id),),
        order_field="created_at",
    )
    user_events_result = (
        await client.schema("public")
        .table("events")
        .select("id", count="exact")
        .eq("session_id", thread_id)
        .eq("author", "user")
        .execute()
    )
    assistant_events = await _fetch_rows_batched(
        lambda: client.schema("public").table("events"),
        select_fields="id,session_id,content,timestamp",
        filters=(("session_id", thread_id), ("author", "assistant")),
        order_field="timestamp",
    )

    user_count = user_events_result.count or len(user_events_result.data or [])
    visible_table_messages = [
        message
        for message in messages
        if not (user_count > 0 and message.get("type") == "user")
    ]
    assistant_fallback_messages = _dedupe_event_fallback_messages(
        table_messages=visible_table_messages,
        fallback_messages=_convert_assistant_events_to_messages(assistant_events),
    )
    return len(visible_table_messages) + user_count + len(assistant_fallback_messages)


def _get_regular_supervisor_wakeup_key() -> str:
    shard_prefix = str(
        os.getenv("REGULAR_SUPERVISOR_SHARD_PREFIX") or "regular-supervisor"
    ).strip()
    if not shard_prefix:
        shard_prefix = "regular-supervisor"
    return f"{shard_prefix}:wakeup"


def _get_regular_supervisor_wakeup_ttl_seconds() -> int:
    raw_value = str(os.getenv("REGULAR_SUPERVISOR_WAKEUP_TTL_SECONDS") or "").strip()
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return 30
    return max(1, parsed_value)


class _RegularAsyncPoolDispatchHandle:
    def send(self) -> None:
        try:
            regular_async_pool_dispatch_actor.send()
        except Exception as dispatch_error:
            logger.warning(
                "Failed to enqueue regular async pool dispatcher actor: %s",
                dispatch_error,
            )

        async def _publish_wakeup() -> None:
            try:
                await redis.publish(
                    REGULAR_ASYNC_POOL_DISPATCH_CHANNEL,
                    "WAKE",
                )
            except Exception as dispatch_error:
                logger.warning(
                    "Failed to publish regular async pool wakeup: %s",
                    dispatch_error,
                )

        try:
            asyncio.create_task(_publish_wakeup())
        except Exception as dispatch_error:
            logger.warning(
                "Failed to schedule regular async pool wakeup: %s",
                dispatch_error,
            )


regular_async_pool_dispatch = _RegularAsyncPoolDispatchHandle()


class _RegularSupervisorDispatchHandle:
    def send(self) -> None:
        if (
            str(os.getenv("REGULAR_SUPERVISOR_PERSISTENT", "false")).strip().lower()
            in _TRUE_ENV_VALUES
        ):

            async def _dispatch_persistent_wakeup() -> None:
                wakeup_key = _get_regular_supervisor_wakeup_key()
                wakeup_ttl_seconds = _get_regular_supervisor_wakeup_ttl_seconds()
                try:
                    wakeup_reserved = await redis.set(
                        wakeup_key,
                        PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID,
                        nx=True,
                        ex=wakeup_ttl_seconds,
                    )
                except Exception as wakeup_reservation_error:
                    logger.warning(
                        "Failed to reserve persistent supervisor wakeup for %s: %s",
                        wakeup_key,
                        wakeup_reservation_error,
                    )
                    wakeup_reserved = True

                if not wakeup_reserved:
                    return

                regular_supervisor_background_actor.send(
                    supervisor_id=PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID,
                )

            try:
                asyncio.create_task(_dispatch_persistent_wakeup())
            except Exception as dispatch_error:
                logger.warning(
                    "Failed to schedule persistent supervisor wakeup dispatch: %s",
                    dispatch_error,
                )
                regular_supervisor_background_actor.send(
                    supervisor_id=PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID,
                )
            return
        supervisor_id = f"regular-supervisor:{uuid.uuid4()}"
        try:
            regular_supervisor_background_actor.send(
                supervisor_id=supervisor_id,
            )
        except Exception as dispatch_error:
            logger.warning(
                "Failed to enqueue dedicated regular supervisor actor %s: %s",
                supervisor_id,
                dispatch_error,
            )
            raise


regular_supervisor_dispatch = _RegularSupervisorDispatchHandle()


def _build_run_capacity_reservation_owner_token(
    *,
    request_id: Optional[str],
    client_operation_id: Optional[str],
) -> str:
    reservation_seed = str(request_id or client_operation_id or uuid.uuid4()).strip()
    return f"api-reservation:{reservation_seed}"


async def _reserve_run_capacity_or_raise(
    *,
    agent_run_id: str,
    mode: Any,
    owner_token: str,
) -> dict[str, Any]:
    reservation_result = await try_acquire_run_capacity_lease(
        agent_run_id=agent_run_id,
        mode=mode,
        owner_token=owner_token,
        allow_takeover=False,
    )
    if bool(reservation_result.get("acquired")):
        return reservation_result
    raise HTTPException(
        status_code=429,
        detail=build_run_capacity_limit_detail(
            mode=mode,
            snapshot=reservation_result,
        ),
    )


async def _release_run_capacity_reservation_best_effort(
    *,
    agent_run_id: str,
    owner_token: Optional[str],
) -> None:
    normalized_owner_token = str(owner_token or "").strip()
    if not normalized_owner_token:
        return
    try:
        await release_run_capacity_lease(
            agent_run_id=agent_run_id,
            owner_token=normalized_owner_token,
        )
    except Exception as release_error:
        logger.warning(
            "Failed to release API-side run capacity reservation for %s: %s",
            agent_run_id,
            release_error,
        )


def _coerce_agent_run_metadata_payload(metadata_value: Any) -> dict[str, Any]:
    if isinstance(metadata_value, dict):
        return metadata_value
    if isinstance(metadata_value, str):
        normalized = metadata_value.strip()
        if not normalized:
            return {}
        try:
            parsed = json.loads(normalized)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _resolve_shadow_clone_v2_execution_chain() -> str:
    normalized_chain = (
        str(os.getenv("SHADOW_CLONE_V2_EXECUTION_CHAIN", "regular_supervisor") or "")
        .strip()
        .lower()
        .replace("-", "_")
    )
    if normalized_chain in {
        "run_agent_background",
        "run_agent",
        "dramatiq",
        "legacy",
        "fallback",
    }:
        return "run_agent_background"
    return "regular_supervisor"


def _should_queue_regular_run(
    mode: Any,
    *,
    shadow_clone_runtime: Any = None,
) -> bool:
    return _resolve_regular_execution_mode(
        mode,
        shadow_clone_runtime=shadow_clone_runtime,
    ) in {
        "phase1_pool",
        "phase2_supervisor",
    }


def _is_shadow_clone_model_policy_mode(mode: Any) -> bool:
    normalized_mode = str(getattr(mode, "value", mode) or "").strip().lower()
    return normalized_mode in {"on", "auto", "v2"}


def _resolve_regular_execution_mode(
    mode: Any,
    *,
    shadow_clone_runtime: Any = None,
) -> str:
    normalized_mode = str(getattr(mode, "value", mode) or "").strip().lower()
    if normalized_mode not in {"", "off"}:
        normalized_runtime = str(shadow_clone_runtime or "").strip().lower()
        if (
            normalized_runtime == "v2"
            and _resolve_shadow_clone_v2_execution_chain() == "regular_supervisor"
        ):
            return "phase2_supervisor"
        return "legacy"
    return regular_run_attempts.get_regular_execution_mode()


def _read_regular_execution_mode_from_metadata(metadata_value: Any) -> str:
    metadata_payload = _coerce_agent_run_metadata_payload(metadata_value)
    return str(metadata_payload.get("regular_execution_mode") or "").strip().lower()


def _read_phase2_pointer_epoch(
    agent_run_row: Dict[str, Any],
    *,
    field_names: tuple[str, ...],
) -> Optional[int]:
    if not isinstance(agent_run_row, dict):
        return None
    for field_name in field_names:
        try:
            candidate_value = int(agent_run_row.get(field_name) or 0)
        except (TypeError, ValueError):
            continue
        if candidate_value > 0:
            return candidate_value
    return None


def _is_queued_regular_agent_run_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    if str(row.get("status") or "").strip().lower() != "queued":
        return False
    metadata_payload = _coerce_agent_run_metadata_payload(row.get("metadata"))
    if (
        str(metadata_payload.get("regular_execution_mode") or "").strip().lower()
        == "phase2_supervisor"
    ):
        return True
    shadow_clone_mode = (
        str(metadata_payload.get("shadow_clone_mode") or "").strip().lower()
    )
    return shadow_clone_mode in {"", "off"}


def _is_phase2_regular_agent_run_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    metadata_payload = _coerce_agent_run_metadata_payload(row.get("metadata"))
    return (
        str(metadata_payload.get("regular_execution_mode") or "").strip().lower()
        == "phase2_supervisor"
    )


async def _read_agent_run_row_for_stop(
    client: Any,
    agent_run_id: str,
) -> Optional[dict[str, Any]]:
    if not hasattr(client, "table"):
        return None

    query_result = await (
        client.table("agent_runs")
        .select("status, metadata")
        .eq("agent_run_id", agent_run_id)
        .execute()
    )
    rows = getattr(query_result, "data", None) or []
    if not rows:
        return None
    return dict(rows[0])


async def _ensure_regular_queue_capacity_or_raise(client: Any) -> None:
    try:
        await regular_async_pool.ensure_regular_queue_capacity(
            client,
            additional_slots=1,
        )
    except regular_async_pool.RegularQueueFullError as queue_full_error:
        raise HTTPException(
            status_code=429,
            detail=str(queue_full_error),
        ) from queue_full_error


async def _ensure_phase2_attempt_queue_capacity_or_raise(client: Any) -> None:
    try:
        await regular_run_attempts.ensure_attempt_queue_capacity(
            client,
            additional_slots=1,
        )
    except regular_run_attempts.RegularAttemptQueueFullError as queue_full_error:
        raise HTTPException(
            status_code=429,
            detail=str(queue_full_error),
        ) from queue_full_error


def _dispatch_regular_queue_handoff(*, execution_mode: str) -> None:
    if execution_mode == "phase2_supervisor":
        regular_supervisor_dispatch.send()
        return
    regular_async_pool_dispatch.send()


async def _mark_phase2_queued_attempts_terminal(
    client: Any,
    *,
    agent_run_id: str,
    final_status: str,
    error_message: Optional[str],
) -> bool:
    if not hasattr(client, "table"):
        return False

    terminal_reason = str(error_message or "").strip() or (
        "external_stop_failed" if error_message else "external_stop"
    )
    queued_result = await (
        client.table("regular_run_attempts")
        .select("attempt_id, status")
        .eq("agent_run_id", agent_run_id)
        .eq("status", "queued")
        .execute()
    )
    queued_rows = list(getattr(queued_result, "data", None) or [])
    if not queued_rows:
        return False

    update_result = await (
        client.table("regular_run_attempts")
        .eq("agent_run_id", agent_run_id)
        .eq("status", "queued")
        .update(
            {
                "status": final_status,
                "terminal_status": final_status,
                "terminal_reason": terminal_reason,
            }
        )
        .execute()
    )
    updated_rows = list(getattr(update_result, "data", None) or [])
    if updated_rows:
        return True

    remaining_result = await (
        client.table("regular_run_attempts")
        .select("attempt_id, status")
        .eq("agent_run_id", agent_run_id)
        .eq("status", "queued")
        .execute()
    )
    remaining_rows = list(getattr(remaining_result, "data", None) or [])
    return not remaining_rows and bool(queued_rows)


async def _mark_phase2_admission_attempt_failed_best_effort(
    client: Any,
    *,
    agent_run_id: str,
    error_message: str,
) -> None:
    try:
        attempt_marked = await _mark_phase2_queued_attempts_terminal(
            client,
            agent_run_id=agent_run_id,
            final_status="failed",
            error_message=error_message,
        )
    except Exception as cleanup_error:
        logger.warning(
            "Failed to mark queued phase2 admission attempt failed for %s: %s",
            agent_run_id,
            cleanup_error,
        )
        return

    if not attempt_marked:
        logger.warning(
            "Queued phase2 admission attempt cleanup did not update any attempt rows for %s",
            agent_run_id,
        )


async def _load_run_capacity_reservation_owner_token_for_stop(
    client: Any,
    agent_run_id: str,
) -> Optional[str]:
    if not is_server_concurrency_budget_enabled():
        return None
    if not hasattr(client, "table"):
        return None

    try:
        query_result = await (
            client.table("agent_runs")
            .select("metadata")
            .eq("agent_run_id", agent_run_id)
            .execute()
        )
    except Exception as query_error:
        logger.warning(
            "Failed to load agent run metadata for capacity reservation release on stop %s: %s",
            agent_run_id,
            query_error,
        )
        return None

    rows = getattr(query_result, "data", None) or []
    if not rows:
        return None

    metadata_payload = _coerce_agent_run_metadata_payload(rows[0].get("metadata"))
    request_id = str(metadata_payload.get("request_id") or "").strip() or None
    client_operation_id = (
        str(metadata_payload.get("client_operation_id") or "").strip() or None
    )
    if request_id is None and client_operation_id is None:
        return None
    return _build_run_capacity_reservation_owner_token(
        request_id=request_id,
        client_operation_id=client_operation_id,
    )


async def _release_api_side_run_capacity_reservation_on_stop_best_effort(
    client: Any,
    agent_run_id: str,
) -> None:
    owner_token = await _load_run_capacity_reservation_owner_token_for_stop(
        client,
        agent_run_id,
    )
    if not owner_token:
        return

    try:
        release_result = await release_run_capacity_lease(
            agent_run_id=agent_run_id,
            owner_token=owner_token,
        )
    except Exception as release_error:
        logger.warning(
            "Failed to release API-side capacity reservation during stop for %s: %s",
            agent_run_id,
            release_error,
        )
        return

    if bool(release_result.get("released")):
        logger.info(
            "Released API-side capacity reservation during stop for %s",
            agent_run_id,
        )
        return
    if bool(release_result.get("owner_conflict")):
        logger.info(
            "Skipping API-side capacity reservation release during stop for %s because worker already took ownership",
            agent_run_id,
        )
        return
    logger.info(
        "No active API-side capacity reservation remained to release during stop for %s",
        agent_run_id,
    )


def determine_sandbox_type(files):
    """
    根据上传的文件类型智能选择沙箱模板

    Args:
        files: 上传的文件列表

    Returns:
        str: 沙箱类型 ('desktop', 'browser', 'code', 'base')
    """
    if not files:
        return "desktop"  # 默认使用桌面模板

    # 分析文件类型
    file_extensions = []
    file_names = []

    for file_obj in files:
        # UploadFile 对象直接使用 .filename 属性
        if hasattr(file_obj, "filename") and file_obj.filename:
            filename = file_obj.filename.lower()
        elif hasattr(file_obj, "get"):
            # 如果是字典格式的文件信息
            filename = file_obj.get("filename", "").lower()
        else:
            # 如果是字符串
            filename = str(file_obj).lower()

        file_names.append(filename)
        if "." in filename:
            ext = filename.split(".")[-1]
            file_extensions.append(ext)

    logger.info(f"Analyzing file types: {file_extensions}")

    # 如果有网页相关文件，使用浏览器模板
    web_extensions = {"html", "htm", "css", "js", "ts", "jsx", "tsx", "vue", "react"}
    if any(ext in web_extensions for ext in file_extensions):
        logger.info("Detected web files, selecting browser template")
        return "browser"

    # 如果只有代码文件且不需要图形界面，使用代码解释器
    code_extensions = {
        "py",
        "ipynb",
        "r",
        "sql",
        "sh",
        "bash",
        "json",
        "yaml",
        "yml",
        "txt",
        "md",
    }
    if any(ext in code_extensions for ext in file_extensions) and not any(
        ext in {"png", "jpg", "jpeg", "gif", "svg", "pdf", "doc", "docx"}
        for ext in file_extensions
    ):
        # 如果有 Jupyter notebook，使用桌面环境以便查看图表
        if any(ext == "ipynb" for ext in file_extensions):
            logger.info("Detected Jupyter notebook, selecting desktop template")
            return "desktop"
        logger.info("Detected pure code files, selecting code interpreter template")
        return "code"

    # 默认使用桌面模板 - 提供最完整的功能
    # 适用于：图像文件、混合文件类型、需要图形界面的场景
    logger.info("Using default desktop template")
    return "desktop"


from utils.constants import MODEL_NAME_ALIASES
from utils.model_resolver import resolve_model_config
from flags.flags import is_enabled

from .media_payload import build_user_event_parts, is_image_upload
from .config_helper import (
    extract_agent_config,
    build_unified_config,
    extract_tools_for_agent_run,
    get_mcp_configs,
)

router = APIRouter()
router.include_router(
    shadow_clone_router,
    prefix="/agent-run",
    tags=["shadow-clone"],
)


_DEEPSEEK_REASONING_MODEL_DEFAULTS = {
    "deepseek-v4-pro-high": "high",
    "deepseek-v4-pro-max": "max",
    "deepseek-v4-flash-high": "high",
    "deepseek-v4-flash-max": "max",
}


def _normalize_model_reasoning_controls(
    model_name: Optional[str],
    enable_thinking: Optional[bool],
    reasoning_effort: Optional[str],
) -> tuple[Optional[bool], Optional[str]]:
    default_effort = _DEEPSEEK_REASONING_MODEL_DEFAULTS.get(
        str(model_name or "").strip().lower()
    )
    if not default_effort:
        return enable_thinking, reasoning_effort
    return True, default_effort


db = None
instance_id = None  # Global instance ID for this backend instance

# TTL for Redis response lists (24 hours)
REDIS_RESPONSE_LIST_TTL = 3600 * 24
QWEN_SINGLE_ACTIVE_WAIT_SECONDS = max(
    0.5,
    float(os.getenv("AGENTSCOPE_QWEN_SINGLE_ACTIVE_WAIT_SECONDS", "2.0")),
)
QWEN_SINGLE_ACTIVE_POLL_SECONDS = 0.2
AGENT_STREAM_INITIAL_REPLAY_LIMIT = max(
    200,
    int(os.getenv("AGENT_STREAM_INITIAL_REPLAY_LIMIT", "1500")),
)
AGENT_STREAM_RUNNING_CATCHUP_LIMIT = max(
    200,
    int(os.getenv("AGENT_STREAM_RUNNING_CATCHUP_LIMIT", "1200")),
)
AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER = max(
    2,
    int(os.getenv("AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER", "4")),
)
AGENT_STREAM_QUEUE_POLL_INTERVAL_SECONDS = max(
    0.1,
    float(os.getenv("AGENT_STREAM_QUEUE_POLL_INTERVAL_SECONDS", "0.25")),
)
AGENT_STREAM_PUBSUB_POLL_INTERVAL_SECONDS = max(
    0.05,
    float(os.getenv("AGENT_STREAM_PUBSUB_POLL_INTERVAL_SECONDS", "0.1")),
)
AGENT_STREAM_PING_INTERVAL_SECONDS = max(
    5.0,
    float(os.getenv("AGENT_STREAM_PING_INTERVAL_SECONDS", "15")),
)
STREAM_TOOL_PROGRESS_TEXT_MIN_BYTES = max(
    32,
    int(os.getenv("STREAM_TOOL_PROGRESS_TEXT_MIN_BYTES", "64")),
)
STREAM_TOOL_PROGRESS_ARGUMENTS_MIN_BYTES = max(
    128,
    int(os.getenv("STREAM_TOOL_PROGRESS_ARGUMENTS_MIN_BYTES", "512")),
)
STREAM_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
STREAM_NON_PROGRESS_METADATA_STATUSES = {
    "heartbeat",
    "shadow_clone_environment_preparing",
    "shadow_clone_environment_ready",
    "shadow_clone_environment_recovering",
    "tool_call_chunk",
}
STREAM_TOOL_LIFECYCLE_STATUS_TYPES = {
    "tool_started",
    "tool_completed",
    "tool_failed",
}
STREAM_NON_PROGRESS_STATUS_TYPES = {
    "tool_call_chunk",
    *STREAM_TOOL_LIFECYCLE_STATUS_TYPES,
}
STREAM_CONTROL_SIGNAL_TO_TERMINAL_STATUS = {
    "END_STREAM": "completed",
    "ERROR": "failed",
    "STOP": "stopped",
}


def _summary_json(value: Dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True)


def _limited_dict_keys(value: Any, limit: int = 8) -> List[str]:
    if not isinstance(value, dict):
        return []
    return [str(key) for key in list(value.keys())[:limit]]


def _summarize_thread_lookup_result(thread_result: Any) -> Dict[str, Any]:
    data = getattr(thread_result, "data", None)
    if not isinstance(data, list):
        return {"field_names": [], "row_count": 0}
    first_row = data[0] if data else {}
    return {
        "field_names": _limited_dict_keys(first_row),
        "row_count": len(data),
    }


def _summarize_agent_record(agent_data: Dict[str, Any]) -> Dict[str, Any]:
    metadata = agent_data.get("metadata")
    return {
        "agent_id": agent_data.get("agent_id"),
        "current_version_id": agent_data.get("current_version_id"),
        "field_count": len(agent_data),
        "has_metadata": isinstance(metadata, dict) and bool(metadata),
        "is_default": bool(agent_data.get("is_default")),
    }


def _summarize_version_record(version_data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(version_data, dict):
        return {"available": False}

    return {
        "available": True,
        "field_count": len(version_data),
        "has_model": bool(version_data.get("model")),
        "has_system_prompt": bool(version_data.get("system_prompt")),
        "version_id": version_data.get("version_id"),
    }


def _summarize_agent_config(agent_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(agent_config, dict):
        return {"available": False}

    tools = agent_config.get("tools")
    mcp_configs = agent_config.get("mcp_configs")
    if isinstance(mcp_configs, dict):
        mcp_config_count = len(mcp_configs)
    elif isinstance(mcp_configs, list):
        mcp_config_count = len(mcp_configs)
    else:
        mcp_config_count = 0

    return {
        "agent_id": agent_config.get("agent_id"),
        "available": True,
        "config_keys": _limited_dict_keys(agent_config),
        "current_version_id": agent_config.get("current_version_id"),
        "has_system_prompt": bool(agent_config.get("system_prompt")),
        "mcp_config_count": mcp_config_count,
        "model": agent_config.get("model"),
        "tool_count": len(tools) if isinstance(tools, list) else 0,
    }


def _summarize_message_content(message_content: str) -> Dict[str, Any]:
    return {
        "attachment_marker_count": message_content.count("[用户上传文件:"),
        "character_count": len(message_content),
        "contains_text": bool(message_content.strip()),
        "line_count": len(message_content.splitlines()),
    }


def _parse_stream_object(value: Any) -> Optional[Dict[str, Any]]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _resolve_stream_terminal_status(status_value: Any) -> Optional[str]:
    normalized = str(status_value or "").strip().lower()
    if normalized == "error":
        return "failed"
    if normalized in STREAM_TERMINAL_STATUSES:
        return normalized
    return None


def _resolve_stream_control_terminal_status(control_signal: Any) -> Optional[str]:
    normalized = str(control_signal or "").strip().upper()
    return STREAM_CONTROL_SIGNAL_TO_TERMINAL_STATUS.get(normalized)


def _extract_stream_metadata(response: Dict[str, Any]) -> Dict[str, Any]:
    metadata = _parse_stream_object(response.get("metadata"))
    return metadata if isinstance(metadata, dict) else {}


def _extract_stream_content(response: Dict[str, Any]) -> Dict[str, Any]:
    content = _parse_stream_object(response.get("content"))
    return content if isinstance(content, dict) else {}


def _extract_tool_call_arguments_text(
    tool_call: Dict[str, Any],
) -> tuple[str, Dict[str, Any]]:
    function_payload = tool_call.get("function")
    if not isinstance(function_payload, dict):
        return "", {}

    raw_arguments = function_payload.get("arguments")
    if isinstance(raw_arguments, dict):
        try:
            return json.dumps(raw_arguments, ensure_ascii=False), raw_arguments
        except Exception:
            return str(raw_arguments), raw_arguments
    if not isinstance(raw_arguments, str):
        return str(raw_arguments or ""), {}

    parsed_arguments = _parse_stream_object(raw_arguments)
    return raw_arguments, parsed_arguments or {}


def _extract_meaningful_tool_text(parsed_arguments: Dict[str, Any]) -> Optional[str]:
    for key in (
        "content",
        "text",
        "body",
        "code",
        "markdown",
        "query",
        "prompt",
        "instructions",
    ):
        value = parsed_arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _is_meaningful_tool_call_chunk(response: Dict[str, Any]) -> bool:
    if response.get("type") != "assistant":
        return False

    metadata = _extract_stream_metadata(response)
    if str(metadata.get("stream_status") or "").strip() != "tool_call_chunk":
        return False

    tool_calls = metadata.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False

    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function_payload = tool_call.get("function")
        if not isinstance(function_payload, dict):
            continue

        arguments_text, parsed_arguments = _extract_tool_call_arguments_text(tool_call)
        meaningful_text = _extract_meaningful_tool_text(parsed_arguments)
        if (
            meaningful_text
            and len(meaningful_text.encode("utf-8"))
            >= STREAM_TOOL_PROGRESS_TEXT_MIN_BYTES
        ):
            return True

        tool_name = str(function_payload.get("name") or "").strip().lower()
        if (
            tool_name == "write_file"
            and len(arguments_text.encode("utf-8"))
            >= STREAM_TOOL_PROGRESS_ARGUMENTS_MIN_BYTES
        ):
            return True

    return False


def _resolve_interactive_shadow_clone_mode_value(
    shadow_clone_mode: Optional[str],
) -> str:
    return resolve_shadow_clone_mode(
        shadow_clone_mode=shadow_clone_mode,
        execution_origin="interactive",
    ).value


def _normalize_agent_backend_runtime(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _is_claude_sdk_backend_value(value: Any) -> bool:
    return _normalize_agent_backend_runtime(value) in {
        "claude",
        "claude_sdk",
        "claude_agent_sdk",
        "claudeagentsdk",
    }


def _agent_config_requests_claude_sdk_runtime(agent_config: Any) -> bool:
    if not isinstance(agent_config, dict):
        return False
    for key in ("agent_backend", "agentBackend", "backend"):
        if _is_claude_sdk_backend_value(agent_config.get(key)):
            return True
    return False


def _resolve_shadow_clone_runtime_value(
    effective_shadow_clone_mode: Any,
    *,
    agent_config: Optional[dict[str, Any]] = None,
) -> str:
    normalized_mode = (
        str(
            getattr(effective_shadow_clone_mode, "value", effective_shadow_clone_mode)
            or ""
        )
        .strip()
        .lower()
    )
    if normalized_mode in {"", "off", "false", "none", "disabled"}:
        return "off"

    normalized_backend = os.getenv("AGENT_BACKEND", "")
    allow_claude_sdk_backend = os.getenv(
        "ALLOW_CLAUDE_SDK_BACKEND", ""
    ).strip().lower() in {"1", "true", "yes"}
    if (
        _is_claude_sdk_backend_value(normalized_backend)
        or _agent_config_requests_claude_sdk_runtime(agent_config)
    ) and allow_claude_sdk_backend:
        return "claude_sdk"

    return "v2"


def _build_terminal_stream_status_payload(
    status_value: str,
    *,
    message: Optional[str] = None,
    control_signal: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"type": "status", "status": status_value}
    if message:
        payload["message"] = message
    if control_signal:
        payload["control_signal"] = control_signal
    return payload


def _is_agent_run_terminal_status(status_value: Any) -> bool:
    return str(status_value or "").strip().lower() in {"completed", "failed", "stopped"}


def _build_phase2_public_event_index(
    *,
    execution_epoch: int,
    local_event_index: int,
) -> int:
    normalized_epoch = max(1, int(execution_epoch or 0))
    normalized_local_event_index = max(0, int(local_event_index or 0))
    return (
        normalized_epoch * _PHASE2_PUBLIC_EVENT_INDEX_EPOCH_STRIDE
    ) + normalized_local_event_index


def _build_public_stream_event_index(
    *,
    local_event_index: int,
    current_execution_epoch: Optional[int],
) -> int:
    normalized_local_event_index = max(0, int(local_event_index or 0))
    normalized_execution_epoch = max(0, int(current_execution_epoch or 0))
    if normalized_execution_epoch < 1:
        return normalized_local_event_index
    return _build_phase2_public_event_index(
        execution_epoch=normalized_execution_epoch,
        local_event_index=normalized_local_event_index,
    )


def _resolve_response_fetch_start_index(
    *,
    requested_from_index: int,
    current_execution_epoch: Optional[int],
) -> int:
    normalized_requested_from_index = max(0, int(requested_from_index or 0))
    normalized_execution_epoch = max(0, int(current_execution_epoch or 0))
    if normalized_execution_epoch < 1:
        return normalized_requested_from_index

    epoch_base = _build_phase2_public_event_index(
        execution_epoch=normalized_execution_epoch,
        local_event_index=0,
    )
    if normalized_requested_from_index < epoch_base:
        return 0
    return max(0, normalized_requested_from_index - epoch_base)


async def _read_projected_terminal_status_from_response_tail(
    agent_run_id: str,
    *,
    current_execution_epoch: Optional[int],
) -> tuple[Optional[str], Optional[str]]:
    include_legacy_run_wide_fallback = current_execution_epoch is None
    try:
        return await read_terminal_status_from_response_tail(
            agent_run_id,
            current_execution_epoch=current_execution_epoch,
            include_legacy_run_wide_fallback=include_legacy_run_wide_fallback,
        )
    except TypeError as projection_error:
        projection_error_message = str(projection_error)
        if (
            "current_execution_epoch" not in projection_error_message
            and "include_legacy_run_wide_fallback" not in projection_error_message
        ):
            raise
        if current_execution_epoch is None:
            return await read_terminal_status_from_response_tail(agent_run_id)
        try:
            return await read_terminal_status_from_response_tail(
                agent_run_id,
                current_execution_epoch=current_execution_epoch,
            )
        except TypeError as fallback_error:
            if "current_execution_epoch" not in str(fallback_error):
                raise
            return await read_terminal_status_from_response_tail(agent_run_id)


async def _resolve_phase2_latest_execution_epoch(
    client: Any,
    *,
    agent_run_id: str,
    log_context: str,
) -> tuple[Optional[int], bool]:
    try:
        latest_attempt = await regular_run_attempts.load_latest_attempt_for_run(
            client,
            agent_run_id=agent_run_id,
        )
    except Exception as attempt_error:
        logger.warning(
            "Failed to resolve latest phase2 attempt for %s " "(agent_run_id=%s): %s",
            log_context,
            agent_run_id,
            attempt_error,
        )
        return None, False

    try:
        resolved_execution_epoch = (
            max(
                0,
                int((latest_attempt or {}).get("execution_epoch") or 0),
            )
            or None
        )
    except (TypeError, ValueError):
        resolved_execution_epoch = None
    return resolved_execution_epoch, True


async def _resolve_phase2_serving_epoch(
    client: Any,
    *,
    agent_run_id: str,
    agent_run_row: Dict[str, Any],
    pointer_fields: tuple[str, ...],
    log_context: str,
) -> tuple[Optional[int], bool]:
    resolved_pointer_epoch = _read_phase2_pointer_epoch(
        agent_run_row,
        field_names=pointer_fields,
    )
    if resolved_pointer_epoch is not None:
        return resolved_pointer_epoch, True

    return await _resolve_phase2_latest_execution_epoch(
        client,
        agent_run_id=agent_run_id,
        log_context=log_context,
    )


async def _project_agent_run_row_terminal_status(
    agent_run_row: Dict[str, Any],
    *,
    client: Any | None = None,
) -> Dict[str, Any]:
    if not isinstance(agent_run_row, dict):
        return agent_run_row

    current_status = str(agent_run_row.get("status") or "").strip().lower()
    if _is_agent_run_terminal_status(current_status):
        return agent_run_row

    agent_run_id = str(
        agent_run_row.get("agent_run_id") or agent_run_row.get("id") or ""
    ).strip()
    if not agent_run_id:
        return agent_run_row

    resolved_execution_epoch: Optional[int] = None
    if (
        _read_regular_execution_mode_from_metadata(agent_run_row.get("metadata"))
        == "phase2_supervisor"
    ):
        resolved_execution_epoch = _read_phase2_pointer_epoch(
            agent_run_row,
            field_names=("stream_source_epoch", "current_execution_epoch"),
        )
        if resolved_execution_epoch is None:
            resolved_client = client
            if resolved_client is None:
                try:
                    resolved_client = await db.client
                except Exception as client_error:
                    logger.warning(
                        "Failed to resolve DB client for phase2 terminal projection "
                        "(agent_run_id=%s): %s",
                        agent_run_id,
                        client_error,
                    )
                    resolved_client = None
            if resolved_client is None:
                return agent_run_row
            (
                resolved_execution_epoch,
                phase2_lookup_succeeded,
            ) = await _resolve_phase2_serving_epoch(
                resolved_client,
                agent_run_id=agent_run_id,
                agent_run_row=agent_run_row,
                pointer_fields=("stream_source_epoch", "current_execution_epoch"),
                log_context="terminal projection",
            )
            if not phase2_lookup_succeeded or resolved_execution_epoch is None:
                return agent_run_row

    projected_status, projected_message = (
        await _read_projected_terminal_status_from_response_tail(
            agent_run_id,
            current_execution_epoch=resolved_execution_epoch,
        )
    )
    if projected_status is None:
        return agent_run_row

    logger.warning(
        "Projecting terminal agent run status from Redis response tail "
        "(agent_run_id=%s db_status=%s projected_status=%s)",
        agent_run_id,
        current_status or None,
        projected_status,
    )
    projected_row = dict(agent_run_row)
    projected_row["status"] = projected_status
    if not projected_row.get("completed_at"):
        projected_row["completed_at"] = projected_row.get("updated_at")
    if (
        projected_status == "failed"
        and not projected_row.get("error")
        and projected_message
    ):
        projected_row["error"] = projected_message
    return projected_row


async def _resolve_public_response_stream_source(
    client: Any,
    *,
    agent_run_id: str,
    agent_run_row: Dict[str, Any],
) -> tuple[Optional[int], Optional[str], bool]:
    if (
        _read_regular_execution_mode_from_metadata(agent_run_row.get("metadata"))
        == "phase2_supervisor"
    ):
        (
            resolved_execution_epoch,
            lookup_succeeded,
        ) = await _resolve_phase2_serving_epoch(
            client,
            agent_run_id=agent_run_id,
            agent_run_row=agent_run_row,
            pointer_fields=("stream_source_epoch", "current_execution_epoch"),
            log_context="stream source",
        )
        if not lookup_succeeded or resolved_execution_epoch is None:
            return resolved_execution_epoch, None, lookup_succeeded
        return (
            resolved_execution_epoch,
            build_response_list_key(
                agent_run_id,
                execution_epoch=resolved_execution_epoch,
            ),
            True,
        )

    return None, build_response_list_key(agent_run_id), True


async def _resolve_phase2_terminal_replay_fallback_source(
    client: Any,
    *,
    agent_run_id: str,
    current_execution_epoch: Optional[int],
) -> tuple[Optional[int], Optional[str]]:
    normalized_execution_epoch = max(0, int(current_execution_epoch or 0))
    if normalized_execution_epoch < 2:
        return None, None

    try:
        result = (
            await client.table("regular_run_attempts")
            .select("attempt_id, attempt_number, execution_epoch, status")
            .eq("agent_run_id", str(agent_run_id or "").strip())
            .execute()
        )
    except Exception as fallback_error:
        logger.warning(
            "Failed to resolve phase2 terminal replay fallback for %s epoch=%s: %s",
            agent_run_id,
            normalized_execution_epoch,
            fallback_error,
        )
        return None, None

    attempt_rows = [dict(row) for row in (getattr(result, "data", None) or [])]
    if not attempt_rows:
        return None, None

    def _sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
        try:
            execution_epoch = int(row.get("execution_epoch") or 0)
        except (TypeError, ValueError):
            execution_epoch = 0
        try:
            attempt_number = int(row.get("attempt_number") or 0)
        except (TypeError, ValueError):
            attempt_number = 0
        return (
            execution_epoch,
            attempt_number,
            str(row.get("attempt_id") or ""),
        )

    latest_attempt_row = max(attempt_rows, key=_sort_key)
    latest_attempt_epoch = max(0, int(latest_attempt_row.get("execution_epoch") or 0))
    if latest_attempt_epoch != normalized_execution_epoch:
        return None, None
    if not _is_agent_run_terminal_status(latest_attempt_row.get("status")):
        return None, None

    previous_attempt_rows = sorted(
        (
            dict(row)
            for row in attempt_rows
            if max(0, int(row.get("execution_epoch") or 0)) < normalized_execution_epoch
            and _is_agent_run_terminal_status(row.get("status"))
        ),
        key=_sort_key,
        reverse=True,
    )
    for previous_attempt_row in previous_attempt_rows:
        previous_epoch = max(0, int(previous_attempt_row.get("execution_epoch") or 0))
        if previous_epoch < 1:
            continue
        previous_response_list_key = build_response_list_key(
            agent_run_id,
            execution_epoch=previous_epoch,
        )
        try:
            preview_payloads = await redis.lrange(previous_response_list_key, 0, 0)
        except Exception as preview_error:
            logger.warning(
                "Failed to inspect phase2 replay fallback source for %s epoch=%s: %s",
                agent_run_id,
                previous_epoch,
                preview_error,
            )
            continue
        if preview_payloads:
            return previous_epoch, previous_response_list_key

    return None, None


def _lease_has_file_delivery_identity(lease: Optional[dict[str, Any]]) -> bool:
    if not isinstance(lease, dict):
        return False
    sandbox_id = str(lease.get("sandbox_id") or "").strip()
    if sandbox_id:
        return True
    manifest = lease.get("environment_manifest")
    if not isinstance(manifest, dict):
        return False
    sandbox = manifest.get("sandbox")
    if not isinstance(sandbox, dict):
        return False
    return bool(str(sandbox.get("id") or "").strip())


def _should_filter_replayed_terminal_status(
    response: Dict[str, Any],
    *,
    authoritative_terminal_status: Optional[str],
) -> bool:
    if authoritative_terminal_status is None:
        return False
    response_terminal_status = _resolve_stream_terminal_status(response.get("status"))
    return (
        response.get("type") == "status"
        and response_terminal_status in STREAM_TERMINAL_STATUSES
    )


def _is_non_progress_stream_response(response: Dict[str, Any]) -> bool:
    if response.get("non_progress") is True:
        return True

    if response.get("type") == "ping":
        return True

    metadata = _extract_stream_metadata(response)
    stream_status = str((metadata or {}).get("stream_status") or "").strip()
    if stream_status == "tool_call_chunk" and _is_meaningful_tool_call_chunk(response):
        return False
    if stream_status in STREAM_NON_PROGRESS_METADATA_STATUSES:
        return True

    if response.get("type") != "status":
        return False

    content = _extract_stream_content(response)
    status_type = str((content or {}).get("status_type") or "").strip()
    return status_type in STREAM_NON_PROGRESS_STATUS_TYPES


def _is_tool_lifecycle_status_response(response: Dict[str, Any]) -> bool:
    if response.get("type") != "status":
        return False

    content = _extract_stream_content(response)
    status_type = str((content or {}).get("status_type") or "").strip()
    return status_type in STREAM_TOOL_LIFECYCLE_STATUS_TYPES


def _decorate_stream_response(response: Dict[str, Any]) -> Dict[str, Any]:
    if not _is_non_progress_stream_response(response):
        return response
    if response.get("non_progress") is True:
        return response
    return {**response, "non_progress": True}


def _select_priority_backlog_window(
    responses: List[Dict[str, Any]],
    *,
    limit: int,
) -> List[Dict[str, Any]]:
    if len(responses) <= limit:
        return responses

    selected_indexes: List[int] = []
    selected_set: set[int] = set()

    for index in range(len(responses) - 1, -1, -1):
        response = responses[index]
        if index in selected_set:
            continue
        if (
            response.get("type") == "status"
            and _resolve_stream_terminal_status(response.get("status")) is not None
        ):
            selected_indexes.append(index)
            selected_set.add(index)
            if len(selected_indexes) >= limit:
                break

    if len(selected_indexes) < limit:
        for index in range(len(responses) - 1, -1, -1):
            if index in selected_set:
                continue
            if not _is_non_progress_stream_response(responses[index]):
                selected_indexes.append(index)
                selected_set.add(index)
                if len(selected_indexes) >= limit:
                    break

    if len(selected_indexes) < limit:
        for index in range(len(responses) - 1, -1, -1):
            if index in selected_set:
                continue
            if _is_tool_lifecycle_status_response(responses[index]):
                continue
            if _is_non_progress_stream_response(responses[index]):
                selected_indexes.append(index)
                selected_set.add(index)
                if len(selected_indexes) >= limit:
                    break

    if not selected_indexes:
        for index in range(len(responses) - 1, -1, -1):
            if index in selected_set:
                continue
            if not _is_tool_lifecycle_status_response(responses[index]):
                continue
            selected_indexes.append(index)
            selected_set.add(index)
            break

    if len(selected_indexes) < limit:
        for index in range(len(responses) - 1, -1, -1):
            if index in selected_set:
                continue
            if _is_tool_lifecycle_status_response(responses[index]):
                continue
            selected_indexes.append(index)
            selected_set.add(index)
            if len(selected_indexes) >= limit:
                break

    selected_indexes.sort()
    return [responses[index] for index in selected_indexes]


def _is_qwen_35_model(model_name: Optional[str]) -> bool:
    if not model_name:
        return False
    normalized = model_name.lower()
    return "qwen3.5-plus" in normalized or "qwen-3.5-plus" in normalized


def _normalize_optional_model_name(value: Optional[str]) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_shadow_clone_model_selection_value(
    value: Optional[str],
    *,
    field_name: str,
    strict: bool,
) -> Optional[str]:
    normalized = _normalize_optional_model_name(value)
    if normalized is None:
        return None

    try:
        return normalize_shadow_clone_selected_model_name(normalized)
    except ValueError as error:
        if strict:
            raise HTTPException(
                status_code=400, detail=f"{field_name} {error}"
            ) from error
        logger.warning(
            "Ignoring unsupported persisted Shadow Clone model selection for %s: %s (%s)",
            field_name,
            normalized,
            error,
        )
        return None


async def _get_user_role_record(client, user_id: str) -> Dict[str, Any]:
    async with client.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, role, status
            FROM users
            WHERE id = $1
            """,
            user_id,
        )

    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    user = dict(row)
    user["id"] = str(user.get("id"))
    return user


async def _resolve_shadow_clone_model_selection(
    *,
    client,
    user_id: str,
    requested_main_model: Optional[str],
    requested_subagent_model: Optional[str],
) -> Dict[str, Optional[str]]:
    requested_main_present = (
        _normalize_optional_model_name(requested_main_model) is not None
    )
    requested_subagent_present = (
        _normalize_optional_model_name(requested_subagent_model) is not None
    )

    if requested_main_present or requested_subagent_present:
        user = await _get_user_role_record(client, user_id)
        if str(user.get("status") or "").strip().lower() != "active":
            raise HTTPException(status_code=403, detail="Account is not active")
        if str(user.get("role") or "").strip().lower() != "admin":
            raise HTTPException(
                status_code=403,
                detail="Admin access required for Shadow Clone model overrides",
            )

    normalized_requested_main = _normalize_shadow_clone_model_selection_value(
        requested_main_model,
        field_name="shadow_clone_main_model",
        strict=requested_main_present,
    )
    normalized_requested_subagent = _normalize_shadow_clone_model_selection_value(
        requested_subagent_model,
        field_name="shadow_clone_subagent_model",
        strict=requested_subagent_present,
    )

    global_config = await read_shadow_clone_model_config(client=client)
    global_main_model = _normalize_shadow_clone_model_selection_value(
        global_config.get("main_model_name"),
        field_name="main_model_name",
        strict=False,
    )
    global_subagent_model = _normalize_shadow_clone_model_selection_value(
        global_config.get("subagent_model_name"),
        field_name="subagent_model_name",
        strict=False,
    )

    return {
        "requested_shadow_clone_main_model": normalized_requested_main,
        "requested_shadow_clone_subagent_model": normalized_requested_subagent,
        "effective_shadow_clone_main_model": (
            normalized_requested_main or global_main_model
        ),
        "effective_shadow_clone_subagent_model": (
            normalized_requested_subagent or global_subagent_model
        ),
    }


class AgentStartRequest(BaseModel):
    model_name: Optional[str] = (
        None  # Will be set from config.MODEL_TO_USE in the endpoint
    )
    enable_thinking: Optional[bool] = False
    reasoning_effort: Optional[str] = "low"
    stream: Optional[bool] = True
    enable_context_manager: Optional[bool] = False
    agent_id: Optional[str] = None  # Custom agent to use
    resume_strategy: Optional[str] = "auto"
    resume_window_minutes: Optional[int] = 1440
    shadow_clone_mode: Optional[str] = None
    shadow_clone_main_model: Optional[str] = None
    shadow_clone_subagent_model: Optional[str] = None


class InitiateAgentResponse(BaseModel):
    thread_id: str
    agent_run_id: Optional[str] = None


class PreparedAttachmentPayload(BaseModel):
    attachment_id: str
    name: str
    original_name: Optional[str] = None
    path: str
    size: int
    content_type: Optional[str] = None
    kind: Optional[str] = None
    mime_type: Optional[str] = None
    filename: Optional[str] = None
    sha256: Optional[str] = None


class PrepareAttachmentsResponse(BaseModel):
    project_id: str
    attachments: List[PreparedAttachmentPayload]


class CreateThreadResponse(BaseModel):
    thread_id: str
    project_id: str


class MessageCreateRequest(BaseModel):
    type: str
    content: str
    is_llm_message: bool = True
    media_refs: Optional[List[Dict[str, Any]]] = None


class AgentCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    system_prompt: Optional[str] = (
        None  # 确保系统提示词是可选的，允许默认使用 FuFanManus 的系统提示词
    )
    model: Optional[str] = None  # 确保模型是可选的
    configured_mcps: Optional[List[Dict[str, Any]]] = []
    custom_mcps: Optional[List[Dict[str, Any]]] = []
    agentpress_tools: Optional[Dict[str, Any]] = {}
    is_default: Optional[bool] = False
    avatar: Optional[str] = None
    avatar_color: Optional[str] = None
    profile_image_url: Optional[str] = None


class AgentVersionResponse(BaseModel):
    version_id: str
    agent_id: str
    version_number: int
    version_name: str
    system_prompt: str
    model: Optional[str] = None  # Add model field
    configured_mcps: List[Dict[str, Any]]
    custom_mcps: List[Dict[str, Any]]
    agentpress_tools: Dict[str, Any]
    is_active: bool
    created_at: str
    updated_at: str
    created_by: Optional[str] = None


class AgentVersionCreateRequest(BaseModel):
    system_prompt: str
    configured_mcps: Optional[List[Dict[str, Any]]] = []
    custom_mcps: Optional[List[Dict[str, Any]]] = []
    agentpress_tools: Optional[Dict[str, Any]] = {}
    version_name: Optional[str] = None  # Custom version name
    description: Optional[str] = None  # Version description


class AgentUpdateRequest(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    configured_mcps: Optional[List[Dict[str, Any]]] = None
    custom_mcps: Optional[List[Dict[str, Any]]] = None
    agentpress_tools: Optional[Dict[str, Any]] = None
    is_default: Optional[bool] = None
    # Deprecated, kept for backward-compat
    avatar: Optional[str] = None
    avatar_color: Optional[str] = None
    # New profile image url
    profile_image_url: Optional[str] = None


class AgentResponse(BaseModel):
    agent_id: str
    account_id: str
    name: str
    description: Optional[str] = None
    system_prompt: str
    configured_mcps: List[Dict[str, Any]]
    custom_mcps: List[Dict[str, Any]]
    agentpress_tools: Dict[str, Any]
    is_default: bool
    avatar: Optional[str] = None
    avatar_color: Optional[str] = None
    profile_image_url: Optional[str] = None
    created_at: str
    updated_at: Optional[str] = None
    is_public: Optional[bool] = False

    tags: Optional[List[str]] = []
    current_version_id: Optional[str] = None
    version_count: Optional[int] = 1
    current_version: Optional[AgentVersionResponse] = None
    metadata: Optional[Dict[str, Any]] = None


class PaginationInfo(BaseModel):
    page: int
    limit: int
    total: int
    pages: int


class AgentsResponse(BaseModel):
    agents: List[AgentResponse]
    pagination: PaginationInfo


class ThreadAgentResponse(BaseModel):
    agent: Optional[AgentResponse]
    source: str  # "thread", "default", "none", "missing"
    message: str


class AgentExportData(BaseModel):
    """Exportable agent configuration data"""

    name: str
    description: Optional[str] = None
    system_prompt: str
    agentpress_tools: Dict[str, Any]
    configured_mcps: List[Dict[str, Any]]
    custom_mcps: List[Dict[str, Any]]
    # Deprecated
    avatar: Optional[str] = None
    avatar_color: Optional[str] = None
    # New
    profile_image_url: Optional[str] = None
    tags: Optional[List[str]] = []
    metadata: Optional[Dict[str, Any]] = None
    export_version: str = "1.1"
    exported_at: str
    exported_by: Optional[str] = None


class AgentImportRequest(BaseModel):
    """Request to import an agent from JSON"""

    import_data: AgentExportData
    import_as_new: bool = True  # Always true, only creating new agents is supported


# Helper for version service
async def _get_version_service():
    from .versioning.version_service import get_version_service

    return await get_version_service()


def _sanitize_uploaded_filename(filename: Optional[str]) -> str:
    candidate = unicodedata.normalize("NFC", str(filename or "").strip())
    candidate = candidate.replace("/", "_").replace("\\", "_")
    candidate = os.path.basename(candidate)
    return candidate or f"attachment-{uuid.uuid4().hex[:8]}"


def _canonicalize_workspace_path(
    filename: Optional[str], existing_paths: set[str]
) -> tuple[str, str]:
    safe_filename = _sanitize_uploaded_filename(filename)
    stem, ext = os.path.splitext(safe_filename)
    stem = stem or "attachment"
    candidate_name = safe_filename
    candidate_path = f"/workspace/{candidate_name}"
    counter = 2

    while candidate_path in existing_paths:
        candidate_name = f"{stem} ({counter}){ext}"
        candidate_path = f"/workspace/{candidate_name}"
        counter += 1

    existing_paths.add(candidate_path)
    return candidate_name, candidate_path


def _build_prepared_attachment_payload(
    *,
    record,
    original_name: str,
    file_content_type: Optional[str] = None,
) -> PreparedAttachmentPayload:
    normalized_name = os.path.basename(record.path)
    content_type = file_content_type or record.content_type
    payload: Dict[str, Any] = {
        "attachment_id": record.artifact_id,
        "name": normalized_name,
        "original_name": original_name,
        "path": record.path,
        "size": record.size_bytes,
        "content_type": content_type,
        "filename": normalized_name,
        "sha256": record.sha256,
    }
    if is_image_upload(content_type or "", normalized_name):
        mime_type = (content_type or "").strip().lower()
        if not mime_type.startswith("image/"):
            mime_type = "image/png"
        payload["kind"] = "image"
        payload["mime_type"] = mime_type
    return PreparedAttachmentPayload(**payload)


def _parse_prepared_attachments_json(
    raw_value: Optional[str],
) -> List[PreparedAttachmentPayload]:
    if not raw_value:
        return []
    try:
        decoded = json.loads(raw_value)
    except json.JSONDecodeError as error:
        raise HTTPException(
            status_code=400, detail=f"Invalid prepared_attachments_json: {error}"
        ) from error

    if not isinstance(decoded, list):
        raise HTTPException(
            status_code=400, detail="prepared_attachments_json must be a JSON array"
        )

    parsed: List[PreparedAttachmentPayload] = []
    for item in decoded:
        try:
            parsed.append(PreparedAttachmentPayload.parse_obj(item))
        except ValidationError as error:
            raise HTTPException(
                status_code=400, detail=f"Invalid prepared attachment payload: {error}"
            ) from error
    return parsed


def _prepared_attachment_to_image_ref(
    attachment: PreparedAttachmentPayload,
) -> Optional[Dict[str, str]]:
    if attachment.kind != "image":
        return None
    mime_type = (attachment.mime_type or attachment.content_type or "").strip().lower()
    if not mime_type.startswith("image/"):
        mime_type = "image/png"
    filename = (
        attachment.filename or attachment.name or os.path.basename(attachment.path)
    )
    return {
        "kind": "image",
        "path": attachment.path,
        "mime_type": mime_type,
        "filename": filename,
        "sha256": attachment.sha256 or "",
    }


async def _create_placeholder_project(
    client: Any,
    *,
    user_id: str,
    name: str = "new conversation",
) -> str:
    project_id = str(uuid.uuid4())
    project = (
        await client.schema("public")
        .table("projects")
        .insert(
            {
                "project_id": project_id,
                "account_id": user_id,
                "name": name,
                "created_at": datetime.now(),
            }
        )
    )
    if not project.data:
        raise Exception("Failed to create project")
    return project_id


async def _load_owned_project(
    client: Any, *, project_id: str, user_id: str
) -> Dict[str, Any]:
    project = (
        await client.schema("public")
        .table("projects")
        .select("*")
        .eq(
            "project_id",
            project_id,
        )
        .eq(
            "account_id",
            user_id,
        )
        .execute()
    )
    if not project.data:
        raise HTTPException(
            status_code=404, detail="Project not found or access denied"
        )
    return project.data[0]


@router.post("/agent/attachments/prepare", response_model=PrepareAttachmentsResponse)
async def prepare_agent_attachments(
    project_id: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    if not files:
        raise HTTPException(status_code=400, detail="At least one file is required")
    if not workspace_artifacts.enabled():
        raise HTTPException(
            status_code=503, detail="Attachment preparation is not available"
        )

    client = await db.client

    if project_id:
        await _load_owned_project(client, project_id=project_id, user_id=user_id)
    else:
        project_id = await _create_placeholder_project(client, user_id=user_id)

    existing_paths = set(
        await workspace_artifacts.collect_file_paths(
            project_id=project_id,
            root_path="/workspace",
            client=client,
        )
    )
    prepared_attachments: List[PreparedAttachmentPayload] = []

    for file in files:
        original_name = _sanitize_uploaded_filename(file.filename)
        try:
            canonical_name, target_path = _canonicalize_workspace_path(
                original_name, existing_paths
            )
            content = await file.read()
            record = await workspace_artifacts.persist_artifact(
                project_id=project_id,
                path=target_path,
                content=content,
                source="agent_api.prepare_attachment",
                client=client,
                created_by_user_id=user_id,
                content_type=file.content_type or None,
                metadata={
                    "filename": canonical_name,
                    "original_filename": original_name,
                    "upload_origin": "attachment_prepare",
                },
            )
            if record is None:
                raise HTTPException(
                    status_code=503, detail="Attachment preparation is not available"
                )
            prepared_attachments.append(
                _build_prepared_attachment_payload(
                    record=record,
                    original_name=original_name,
                    file_content_type=file.content_type or None,
                )
            )
        finally:
            await file.close()

    return PrepareAttachmentsResponse(
        project_id=project_id,
        attachments=prepared_attachments,
    )


def initialize(_db: DBConnection, _instance_id: Optional[str] = None):
    """Initialize the agent API with resources from the main API."""
    global db, instance_id
    db = _db

    # Initialize the versioning module with the same database connection
    # initialize_versioning(_db)

    # Use provided instance_id or generate a new one
    if _instance_id:
        instance_id = _instance_id
    else:
        # Generate instance ID
        instance_id = str(uuid.uuid4())[:8]

    logger.info(f"Initialized agent API with instance ID: {instance_id}")
    if regular_async_pool.is_regular_async_pool_enabled("off"):
        try:
            regular_async_pool_dispatch.send()
        except Exception as bootstrap_error:
            logger.warning(
                "Failed to bootstrap regular async pool dispatch on agent API init: %s",
                bootstrap_error,
            )


async def cleanup():
    """Clean up resources and stop running agents on shutdown."""
    logger.info("Starting cleanup of agent API resources")

    # Use the instance_id to find and clean up this instance's keys
    try:
        if instance_id:  # Ensure instance_id is set
            running_keys = await redis.scan_keys(f"active_run:{instance_id}:*")
            logger.info(
                f"Found {len(running_keys)} running agent runs for instance {instance_id} to clean up"
            )

            for key in running_keys:
                # Key format: active_run:{instance_id}:{agent_run_id}
                parts = key.split(":")
                if len(parts) == 3:
                    agent_run_id = parts[2]
                    await stop_agent_run(
                        agent_run_id,
                        error_message=f"Instance {instance_id} shutting down",
                    )
                else:
                    logger.warning(f"Unexpected key format found: {key}")
        else:
            logger.warning(
                "Instance ID not set, cannot clean up instance-specific agent runs."
            )

    except Exception as e:
        logger.error(f"Failed to clean up running agent runs: {str(e)}")

    # Close Redis connection
    await redis.close()
    logger.info("Completed cleanup of agent API resources")


async def _enforce_single_active_qwen_run(
    client,
    thread_id: str,
) -> None:
    running_runs = await (
        client.table("agent_runs")
        .select("agent_run_id, id, status")
        .eq("thread_id", thread_id)
        .eq("status", "running")
        .order("created_at", desc=False)
        .execute()
    )
    active_runs = running_runs.data or []
    if not active_runs:
        return

    active_run_ids = [
        str(run.get("agent_run_id") or run.get("id"))
        for run in active_runs
        if run.get("agent_run_id") or run.get("id")
    ]
    logger.warning(
        "Qwen single-active guard found %s running runs for thread %s: %s",
        len(active_run_ids),
        thread_id,
        active_run_ids,
    )

    for run_id in active_run_ids:
        try:
            await stop_agent_run(run_id)
        except Exception as stop_error:
            logger.warning(
                "Failed to stop existing run %s before starting a new Qwen run on thread %s: %s",
                run_id,
                thread_id,
                stop_error,
            )

    deadline = time.monotonic() + QWEN_SINGLE_ACTIVE_WAIT_SECONDS
    while time.monotonic() < deadline:
        pending = await (
            client.table("agent_runs")
            .select("agent_run_id, id, status")
            .eq("thread_id", thread_id)
            .eq("status", "running")
            .execute()
        )
        pending_rows = pending.data or []
        active_keys: List[str] = []
        for run_id in active_run_ids:
            try:
                key_matches = await redis.scan_keys(f"active_run:*:{run_id}")
            except Exception as redis_error:
                logger.warning(
                    "Failed checking active_run keys for run %s during qwen single-active wait: %s",
                    run_id,
                    redis_error,
                )
                key_matches = []
            if key_matches:
                active_keys.extend([str(key) for key in key_matches])

        if not pending_rows and not active_keys:
            return
        await asyncio.sleep(QWEN_SINGLE_ACTIVE_POLL_SECONDS)

    raise HTTPException(
        status_code=409,
        detail=(
            "A previous run on this thread is still stopping. "
            "Please retry in a few seconds."
        ),
    )


async def stop_agent_run(agent_run_id: str, error_message: Optional[str] = None):
    """
    1. 更新数据库状态
    2. 发布Redis STOP信号
    3. 清理Redis响应列表
    """
    client = None
    try:
        client = await db.client
    except Exception as client_error:
        logger.warning(
            "Failed to acquire database client for stop request %s; "
            "continuing STOP signal delivery without immediate DB sync: %s",
            agent_run_id,
            client_error,
        )
    final_status = "failed" if error_message else "stopped"
    is_phase2_regular_run = False

    if client is not None:
        queued_regular_run = await _read_agent_run_row_for_stop(client, agent_run_id)
        is_phase2_regular_run = _is_phase2_regular_agent_run_row(queued_regular_run)
        if _is_queued_regular_agent_run_row(queued_regular_run):
            queued_regular_execution_mode = _read_regular_execution_mode_from_metadata(
                queued_regular_run.get("metadata")
                if isinstance(queued_regular_run, dict)
                else None
            )
            try:
                update_success = await update_agent_run_status(
                    client, agent_run_id, final_status, error=error_message
                )
            except Exception as update_error:
                logger.warning(
                    "Failed to persist queued regular stop status for %s; "
                    "falling back to legacy STOP flow: %s",
                    agent_run_id,
                    update_error,
                )
            else:
                if update_success:
                    if queued_regular_execution_mode == "phase2_supervisor":
                        try:
                            attempt_update_success = (
                                await _mark_phase2_queued_attempts_terminal(
                                    client,
                                    agent_run_id=agent_run_id,
                                    final_status=final_status,
                                    error_message=error_message,
                                )
                            )
                        except Exception as attempt_update_error:
                            logger.warning(
                                "Failed to persist queued phase2 attempt stop status for %s; "
                                "falling back to legacy STOP flow: %s",
                                agent_run_id,
                                attempt_update_error,
                            )
                        else:
                            if not attempt_update_success:
                                logger.warning(
                                    "No queued phase2 attempt rows were updated during stop for %s; "
                                    "falling back to legacy STOP flow",
                                    agent_run_id,
                                )
                            else:
                                await _release_api_side_run_capacity_reservation_on_stop_best_effort(
                                    client,
                                    agent_run_id,
                                )
                                logger.info(
                                    "Stopped queued phase2 regular run without STOP publish: %s",
                                    agent_run_id,
                                )
                                return

                    if queued_regular_execution_mode != "phase2_supervisor":
                        await _release_api_side_run_capacity_reservation_on_stop_best_effort(
                            client,
                            agent_run_id,
                        )
                        logger.info(
                            "Stopped queued regular run without STOP publish: %s",
                            agent_run_id,
                        )
                        return

                    await _release_api_side_run_capacity_reservation_on_stop_best_effort(
                        client,
                        agent_run_id,
                    )
                logger.warning(
                    "Failed to persist queued regular stop status for %s; "
                    "falling back to legacy STOP flow",
                    agent_run_id,
                )

        try:
            update_success = await update_agent_run_status(
                client, agent_run_id, final_status, error=error_message
            )
        except Exception as update_error:
            logger.warning(
                "Failed to persist terminal database status for stop request %s; "
                "continuing STOP signal delivery: %s",
                agent_run_id,
                update_error,
            )
        else:
            if not update_success:
                logger.warning(
                    "Failed to persist terminal database status for stop request %s; "
                    "continuing STOP signal delivery",
                    agent_run_id,
                )

        if is_phase2_regular_run:
            try:
                await regular_run_attempts.mark_current_attempt_terminal(
                    client,
                    agent_run_id=agent_run_id,
                    final_status=final_status,
                    error_message=error_message,
                )
            except Exception as attempt_terminal_error:
                logger.warning(
                    "Failed to mark current phase2 attempt terminal for stop request %s; "
                    "continuing STOP signal delivery: %s",
                    agent_run_id,
                    attempt_terminal_error,
                )
            try:
                await _mark_phase2_queued_attempts_terminal(
                    client,
                    agent_run_id=agent_run_id,
                    final_status=final_status,
                    error_message=error_message,
                )
            except Exception as queued_attempt_terminal_error:
                logger.warning(
                    "Failed to mark queued phase2 attempts terminal for stop request %s; "
                    "continuing STOP signal delivery: %s",
                    agent_run_id,
                    queued_attempt_terminal_error,
                )

        await _release_api_side_run_capacity_reservation_on_stop_best_effort(
            client,
            agent_run_id,
        )

    shadow_state: Optional[Dict[str, Any]] = None
    updated_lease: Optional[Dict[str, Any]] = None
    shadow_sync_error: Optional[Exception] = None
    if not is_phase2_regular_run:
        shadow_clone_terminal_status = "failed" if error_message else "cancelled"
        shadow_clone_stop_reason = (
            "external_stop_failed" if error_message else "external_stop"
        )
        try:
            shadow_state = await force_terminal_state(
                agent_run_id,
                status=shadow_clone_terminal_status,
                reason=shadow_clone_stop_reason,
                error_message=error_message,
            )
            lease_patch: Dict[str, Any] = {
                "binding_state": "released",
                "environment_ready": False,
                "environment_status": shadow_clone_terminal_status,
                "last_error": (str(error_message or "").strip() or None),
            }
            if isinstance(shadow_state, dict):
                terminal_epoch = max(0, int(shadow_state.get("execution_epoch") or 0))
                lease_patch["execution_epoch"] = terminal_epoch
                lease_patch["lease_epoch"] = terminal_epoch
            updated_lease = await update_run_sandbox_lease(
                agent_run_id,
                **lease_patch,
            )
            if shadow_state is not None or updated_lease is not None:
                logger.info(
                    "[ShadowClone] External stop synchronized terminal shadow state run_id=%s "
                    "shadow_status=%s execution_epoch=%s lease_updated=%s",
                    agent_run_id,
                    shadow_clone_terminal_status,
                    (
                        int(shadow_state.get("execution_epoch") or 0)
                        if isinstance(shadow_state, dict)
                        else None
                    ),
                    updated_lease is not None,
                )
        except Exception as shadow_stop_error:
            shadow_sync_error = shadow_stop_error
            logger.error(
                "[ShadowClone] Failed to synchronize external stop shadow state run_id=%s "
                "parent_status=%s shadow_status=%s stop_reason=%s: %s",
                agent_run_id,
                final_status,
                shadow_clone_terminal_status,
                shadow_clone_stop_reason,
                shadow_stop_error,
                exc_info=True,
            )

        if shadow_sync_error is not None:
            logger.warning(
                "[ShadowClone] External stop will rely on parent-run public projection until shadow state converges "
                "run_id=%s parent_status=%s lease_updated=%s",
                agent_run_id,
                final_status,
                updated_lease is not None,
            )

    # 发送STOP信号到全局控制频道
    global_control_channel = f"agent_run:{agent_run_id}:control"
    try:
        await redis.publish(global_control_channel, "STOP")
        logger.debug(
            f"Published STOP signal to global channel {global_control_channel}"
        )
    except Exception as e:
        logger.error(
            f"Failed to publish STOP signal to global channel {global_control_channel}: {str(e)}"
        )

    # 找到所有处理这个agent_run的实例，并发送STOP信号到实例特定的频道
    try:
        instance_keys = await redis.scan_keys(f"active_run:*:{agent_run_id}")
        for key in instance_keys:
            # Key format: active_run:{instance_id}:{agent_run_id}
            parts = key.split(":")
            if len(parts) == 3:
                instance_id_from_key = parts[1]
                instance_control_channel = (
                    f"agent_run:{agent_run_id}:control:{instance_id_from_key}"
                )
                try:
                    await redis.publish(instance_control_channel, "STOP")
                    logger.debug(
                        f"Published STOP signal to instance channel {instance_control_channel}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to publish STOP signal to instance channel {instance_control_channel}: {str(e)}"
                    )
            else:
                logger.warning(f"Unexpected key format found: {key}")

        # 清理Redis响应列表
        await _cleanup_redis_response_list(agent_run_id)

    except Exception as e:
        logger.error(
            f"Failed to find or signal active instances for {agent_run_id}: {str(e)}"
        )

    logger.info(f"Successfully initiated stop process for agent run: {agent_run_id}")


async def get_agent_run_with_access_check(client, agent_run_id: str, user_id: str):
    """
    1. 查询 agent_run 记录：根据 agent_run_id 从 agent_runs 表中查找对应的 agent 运行记录
    2. 获取关联的 thread 信息：通过 thread_id 查询对应的线程记录
    3. 权限验证：检查当前用户是否有权限访问这个 agent_run
    """
    # 先查询 agent_run，使用新的 agent_run_id 字段
    agent_run = (
        await client.table("agent_runs")
        .select("*")
        .eq("agent_run_id", agent_run_id)
        .execute()
    )
    if not agent_run.data:
        raise HTTPException(status_code=404, detail="Agent run not found")

    agent_run_data = agent_run.data[0]
    thread_id = agent_run_data["thread_id"]

    # 再查询对应的 thread 来获取 account_id
    thread_result = (
        await client.table("threads")
        .select("account_id")
        .eq("thread_id", thread_id)
        .execute()
    )
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")

    # 如果 agent_run 的 account_id 与 user_id 相同，则直接返回 agent_run_data
    account_id = thread_result.data[0]["account_id"]
    if account_id == user_id:
        return agent_run_data

    # 如果 agent_run 的 account_id 与 user_id 不同，则需要验证用户是否有权限访问这个 agent_run，此逻辑用于扩展更丰富的权限控制
    await verify_thread_access(client, thread_id, user_id)
    return agent_run_data


@router.post("/thread/{thread_id}/agent/start")
async def start_agent(
    thread_id: str,
    body: AgentStartRequest = Body(...),
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """Start an agent for a specific thread in the background"""
    structlog.contextvars.bind_contextvars(
        thread_id=thread_id,
    )

    logger.info(f"Starting continue chat with exsting thread: {thread_id}")

    global instance_id
    if not instance_id:
        raise HTTPException(
            status_code=500, detail="Agent API not initialized with instance ID"
        )

    # 使用配置中的模型，如果请求中没有指定
    model_name = body.model_name
    logger.info(f"Original model_name from request: {model_name}")

    if model_name is None:
        model_name = config.MODEL_TO_USE
        logger.info(f"Using model from config: {model_name}")

    # 获取模型别名
    resolved_model = MODEL_NAME_ALIASES.get(model_name, model_name)
    logger.info(f"Resolved model name: {resolved_model}")

    # 根据别名更新模型名称
    model_name = resolved_model

    logger.info(
        f"Starting new agent for thread: {thread_id} with config: model={model_name}, thinking={body.enable_thinking}, effort={body.reasoning_effort}, stream={body.stream}, context_manager={body.enable_context_manager} (Instance: {instance_id})"
    )

    # 获取数据库连接
    client = await db.client

    # 获取线程信息
    thread_result = (
        await client.table("threads")
        .select("project_id, account_id, metadata")
        .eq("thread_id", thread_id)
        .execute()
    )
    logger.info(
        f"Thread lookup summary: {_summary_json(_summarize_thread_lookup_result(thread_result))}"
    )
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")

    thread_data = thread_result.data[0]
    project_id = thread_data.get("project_id")
    account_id = thread_data.get("account_id")
    thread_metadata = thread_data.get("metadata", {})

    if account_id != user_id:
        await verify_thread_access(client, thread_id, user_id)

    structlog.contextvars.bind_contextvars(
        project_id=project_id,
        account_id=account_id,
        thread_metadata=thread_metadata,
    )

    # # Check if this is an agent builder thread
    # is_agent_builder = thread_metadata.get('is_agent_builder', False)
    # target_agent_id = thread_metadata.get('target_agent_id')

    # if is_agent_builder:
    #     logger.info(f"Thread {thread_id} is in agent builder mode, target_agent_id: {target_agent_id}")

    # 加载agent配置，支持版本管理
    agent_config = None
    effective_agent_id = body.agent_id  # Optional agent ID from request

    logger.info(f"[AGENT LOAD] Agent loading flow:")
    logger.info(f"body.agent_id: {body.agent_id}")
    logger.info(f"effective_agent_id: {effective_agent_id}")

    async def load_default_agent():
        """Load the default agent (FuFanManus first, then user default), creating one if needed."""
        # 优先查找FuFanManus默认Agent，如果没有再查找普通默认Agent
        fufanmanus_agent_result = (
            await client.table("agents")
            .select("*")
            .eq("user_id", user_id)
            .eq("metadata->>'is_fufanmanus_default'", "true")
            .execute()
        )

        if fufanmanus_agent_result.data:
            logger.info(
                f"Found FuFanManus default agent: {len(fufanmanus_agent_result.data)} agents"
            )
            default_agent_result = fufanmanus_agent_result
        else:
            # 回退到普通默认Agent查询
            logger.info(f"No FuFanManus agent found, querying regular default agent")
            default_agent_result = (
                await client.schema("public")
                .table("agents")
                .select("*")
                .eq("user_id", user_id)
                .eq("is_default", True)
                .execute()
            )
            logger.info(
                f"Default agent query result: found {len(default_agent_result.data) if default_agent_result.data else 0} default agents"
            )

        if default_agent_result.data:
            agent_data_local = default_agent_result.data[0]
            logger.info(
                f"Found default agent: {agent_data_local.get('name', 'Unknown')} (ID: {agent_data_local.get('agent_id')})"
            )

            # 使用版本系统获取当前版本（做版本控制）
            version_data_local = None
            if agent_data_local.get("current_version_id"):
                try:
                    logger.info(
                        f"Get default agent version data: {agent_data_local['current_version_id']}"
                    )
                    version_service = await _get_version_service()
                    version_obj = await version_service.get_version(
                        agent_id=agent_data_local["agent_id"],
                        version_id=agent_data_local["current_version_id"],
                        user_id=user_id,
                    )
                    version_data_local = version_obj.to_dict()
                    logger.info(
                        f"Get default agent version data: {version_data_local.get('version_name')}"
                    )
                except Exception as e:
                    logger.warning(f"Get default agent version data failed: {e}")

            logger.info(
                f"Prepare to call extract_agent_config for default agent, whether there is version data: {version_data_local is not None}"
            )
            return extract_agent_config(agent_data_local, version_data_local)

        # 自动创建FuFanManus默认Agent（兜底）
        logger.warning(f"User {user_id} not found default agent")
        logger.info(f"Creating FuFanManus default agent for user {user_id}")
        try:
            from agent.fufanmanus.repository import FufanmanusAgentRepository

            repository = FufanmanusAgentRepository()
            created_agent_id = await repository.create_fufanmanus_agent(user_id)

            if created_agent_id:
                # 重新查询刚创建的默认Agent
                default_agent_result = (
                    await client.schema("public")
                    .table("agents")
                    .select("*")
                    .eq("user_id", user_id)
                    .eq("is_default", True)
                    .execute()
                )
                if default_agent_result.data:
                    agent_data_local = default_agent_result.data[0]
                    logger.info(
                        f"Created FuFanManus default agent: {agent_data_local.get('name', 'Unknown')} (ID: {agent_data_local.get('agent_id')})"
                    )
                    version_data_local = None
                    return extract_agent_config(agent_data_local, version_data_local)
                logger.error(f"Failed to query created FuFanManus default agent")
            else:
                logger.error(f"FuFanManus repository returned no agent_id")
        except Exception as e:
            logger.error(f"Failed to create FuFanManus default agent: {e}")
        return None

    if effective_agent_id:
        logger.info(f"[AGENT LOAD] Querying for agent: {effective_agent_id}")
        # 查询agent实例
        agent_result = (
            await client.table("agents")
            .select("*")
            .eq("agent_id", effective_agent_id)
            .eq("user_id", user_id)
            .execute()
        )
        logger.info(
            f"[AGENT LOAD] Query result: found {len(agent_result.data) if agent_result.data else 0} agents"
        )

        if agent_result.data:
            agent_data = agent_result.data[0]
            logger.info(
                f"[AGENT INITIATE] Agent record summary: {_summary_json(_summarize_agent_record(agent_data))}"
            )

            # 使用版本管理系统获取当前版本
            version_data = None
            if agent_data.get("current_version_id"):
                try:
                    version_service = await _get_version_service()
                    version_obj = await version_service.get_version(
                        agent_id=effective_agent_id,
                        version_id=agent_data["current_version_id"],
                        user_id=user_id,
                    )
                    version_data = version_obj.to_dict()
                    logger.info(
                        f"[AGENT INITIATE] Got version data from version manager: {version_data.get('version_name')}"
                    )
                    logger.info(
                        f"[AGENT INITIATE] Version record summary: {_summary_json(_summarize_version_record(version_data))}"
                    )
                except Exception as e:
                    logger.warning(f"[AGENT INITIATE] Failed to get version data: {e}")

            logger.info(
                f"[AGENT INITIATE] About to call extract_agent_config with version data: {version_data is not None}"
            )

            agent_config = extract_agent_config(agent_data, version_data)
            logger.info(
                f"Start agent config summary: {_summary_json(_summarize_agent_config(agent_config))}"
            )
            if version_data:
                logger.info(
                    f"Start agent Using custom agent: {agent_config['name']} ({effective_agent_id}) version {agent_config.get('version_name', 'v1')}"
                )
            else:
                logger.info(
                    f"Start agent Using custom agent: {agent_config['name']} ({effective_agent_id}) - no version data"
                )
        else:
            logger.warning(
                f"[AGENT LOAD] Agent {effective_agent_id} not found for user {user_id}, falling back to default agent"
            )
            agent_config = await load_default_agent()
    else:
        logger.info(f"No agent_id provided, querying default agent")
        agent_config = await load_default_agent()

    if agent_config:
        logger.info(
            f"Loaded agent config summary: {_summary_json(_summarize_agent_config(agent_config))}"
        )

    shadow_clone_model_selection = await _resolve_shadow_clone_model_selection(
        client=client,
        user_id=user_id,
        requested_main_model=body.shadow_clone_main_model,
        requested_subagent_model=body.shadow_clone_subagent_model,
    )

    effective_model = model_name
    if not model_name and agent_config and agent_config.get("model"):
        effective_model = agent_config["model"]
        logger.info(
            f"No model specified by user, using agent's configured model: {effective_model}"
        )
    elif model_name:
        logger.info(f"Using user-selected model: {effective_model}")
    else:
        logger.info(f"Using default model: {effective_model}")

    effective_shadow_clone_mode = _resolve_interactive_shadow_clone_mode_value(
        body.shadow_clone_mode,
    )
    effective_shadow_clone_runtime = _resolve_shadow_clone_runtime_value(
        effective_shadow_clone_mode,
        agent_config=agent_config,
    )
    regular_execution_mode = _resolve_regular_execution_mode(
        effective_shadow_clone_mode,
        shadow_clone_runtime=effective_shadow_clone_runtime,
    )
    queue_regular_run = _should_queue_regular_run(
        effective_shadow_clone_mode,
        shadow_clone_runtime=effective_shadow_clone_runtime,
    )
    shadow_clone_requested_mode = effective_shadow_clone_mode
    qwen_guard_model = (
        shadow_clone_model_selection["effective_shadow_clone_main_model"]
        if _is_shadow_clone_model_policy_mode(shadow_clone_requested_mode)
        else None
    ) or effective_model
    if _is_qwen_35_model(qwen_guard_model):
        await _enforce_single_active_qwen_run(client, thread_id)

    requested_run_capacity_cost = get_run_capacity_cost(effective_shadow_clone_mode)
    if is_server_concurrency_budget_enabled() and not queue_regular_run:
        capacity_snapshot = await get_safe_run_capacity_snapshot(
            mode=effective_shadow_clone_mode,
            requested_cost=requested_run_capacity_cost,
        )
        if not bool(capacity_snapshot.get("can_admit")):
            raise HTTPException(
                status_code=429,
                detail=build_run_capacity_limit_detail(
                    mode=effective_shadow_clone_mode,
                    snapshot=capacity_snapshot,
                ),
            )

    request_context = structlog.contextvars.get_contextvars()
    request_id = request_context.get("request_id")
    client_operation_id = request_context.get("client_operation_id")
    agent_run_id = str(uuid.uuid4())
    run_capacity_reservation_owner = ""
    run_capacity_reservation_acquired = False
    agent_run_status_updated = False
    phase2_initial_attempt_created = False

    agent_run_metadata = {
        "model_name": effective_model,
        "requested_model": model_name,
        "enable_thinking": body.enable_thinking,
        "reasoning_effort": body.reasoning_effort,
        "enable_context_manager": body.enable_context_manager,
        "resume_strategy": body.resume_strategy,
        "resume_window_minutes": body.resume_window_minutes,
        "requested_shadow_clone_mode": body.shadow_clone_mode,
        "shadow_clone_mode": effective_shadow_clone_mode,
        "shadow_clone_runtime": effective_shadow_clone_runtime,
        "requested_shadow_clone_main_model": shadow_clone_model_selection[
            "requested_shadow_clone_main_model"
        ],
        "requested_shadow_clone_subagent_model": shadow_clone_model_selection[
            "requested_shadow_clone_subagent_model"
        ],
        "shadow_clone_main_model": shadow_clone_model_selection[
            "effective_shadow_clone_main_model"
        ],
        "shadow_clone_subagent_model": shadow_clone_model_selection[
            "effective_shadow_clone_subagent_model"
        ],
    }
    if request_id:
        agent_run_metadata["request_id"] = request_id
    if client_operation_id:
        agent_run_metadata["client_operation_id"] = client_operation_id
    if agent_config:
        agent_run_metadata["agent_config_snapshot"] = agent_config
    if effective_shadow_clone_runtime == "v2":
        agent_run_metadata["shadow_clone_v2_execution_chain"] = (
            _resolve_shadow_clone_v2_execution_chain()
        )
    if regular_execution_mode == "phase1_pool":
        agent_run_metadata["regular_async_pool"] = True
    elif regular_execution_mode == "phase2_supervisor":
        agent_run_metadata["regular_execution_mode"] = regular_execution_mode

    if regular_execution_mode == "phase2_supervisor":
        pass
    elif queue_regular_run:
        await _ensure_regular_queue_capacity_or_raise(client)
    elif is_server_concurrency_budget_enabled():
        run_capacity_reservation_owner = _build_run_capacity_reservation_owner_token(
            request_id=request_id,
            client_operation_id=client_operation_id,
        )
        await _reserve_run_capacity_or_raise(
            agent_run_id=agent_run_id,
            mode=effective_shadow_clone_mode,
            owner_token=run_capacity_reservation_owner,
        )
        run_capacity_reservation_acquired = True

    if queue_regular_run and regular_execution_mode == "phase2_supervisor":
        return await regular_run_admission.admit_queued_regular_run(
            client=client,
            thread_id=thread_id,
            project_id=project_id,
            agent_run_id=agent_run_id,
            agent_config_snapshot=agent_config,
            agent_run_metadata=agent_run_metadata,
            execution_mode=regular_execution_mode,
            ensure_phase2_attempt_queue_capacity=_ensure_phase2_attempt_queue_capacity_or_raise,
            create_initial_attempt=regular_run_attempts.create_initial_attempt,
            dispatch_queue_handoff=_dispatch_regular_queue_handoff,
            mark_phase2_admission_attempt_failed=_mark_phase2_admission_attempt_failed_best_effort,
            update_agent_run_status=update_agent_run_status,
        )

    try:
        initial_status = "queued" if queue_regular_run else "running"
        agent_run = (
            await client.schema("public")
            .table("agent_runs")
            .insert(
                {
                    "thread_id": thread_id,
                    "agent_run_id": agent_run_id,
                    "status": initial_status,
                    "started_at": datetime.now(),
                    "agent_id": agent_config.get("agent_id") if agent_config else None,
                    "agent_version_id": (
                        agent_config.get("current_version_id") if agent_config else None
                    ),
                    "metadata": json.dumps(agent_run_metadata),
                }
            )
        )

        agent_run_id = str(
            agent_run.data[0].get("agent_run_id") or agent_run.data[0]["id"]
        )
        structlog.contextvars.bind_contextvars(
            agent_run_id=agent_run_id,
        )
        logger.info(f"Created new agent run: {agent_run_id}")

        if queue_regular_run:
            _dispatch_regular_queue_handoff(execution_mode=regular_execution_mode)
            return {"agent_run_id": agent_run_id, "status": "queued"}

        instance_key = f"active_run:{instance_id}:{agent_run_id}"
        try:
            await redis.set(instance_key, "running", ex=redis.REDIS_KEY_TTL)
        except Exception as e:
            logger.warning(
                f"Failed to register agent run in Redis ({instance_key}): {str(e)}"
            )

        logger.info(
            f"Start agent run: agent_run_id={agent_run_id} request_id={request_id or '-'} "
            f"client_operation_id={client_operation_id or '-'} model_name={effective_model} "
            f"enable_thinking={body.enable_thinking} reasoning_effort={body.reasoning_effort} "
            f"stream={body.stream} enable_context_manager={body.enable_context_manager}"
        )
        logger.info(
            f"Start agent config summary: {_summary_json(_summarize_agent_config(agent_config))}"
        )

        # 🔧 等待并验证用户消息已保存到数据库（带重试机制）
        # 这解决了时序竞争问题：前端调用 /threads/{thread_id}/messages 后立即调用 /agent/start
        logger.info("Waiting for user message to be saved to database...")

        message_found = False
        max_retries = 10
        retry_delay = 0.1  # 100ms per retry, total max wait = 1 second

        for attempt in range(max_retries):
            try:
                events_result = (
                    await client.schema("public")
                    .table("events")
                    .select("id, timestamp")
                    .eq("session_id", thread_id)
                    .eq("author", "user")
                    .order("timestamp", desc=True)
                    .limit(1)
                    .execute()
                )

                if events_result.data:
                    latest_message_time = events_result.data[0]["timestamp"]
                    logger.info(
                        f"✅ Latest user message found on attempt {attempt + 1}: {latest_message_time}"
                    )
                    message_found = True
                    break
                else:
                    logger.debug(
                        f"Attempt {attempt + 1}/{max_retries}: No user messages found yet, retrying..."
                    )
                    await asyncio.sleep(retry_delay)

            except Exception as check_error:
                logger.warning(
                    f"Attempt {attempt + 1}/{max_retries}: Error checking message: {check_error}"
                )
                await asyncio.sleep(retry_delay)

        if not message_found:
            logger.warning(
                f"⚠️ User message not found after {max_retries} attempts. Proceeding anyway..."
            )

        run_agent_background.send(
            agent_run_id=agent_run_id,
            thread_id=thread_id,
            instance_id=instance_id,
            project_id=project_id,
            model_name=effective_model,
            enable_thinking=body.enable_thinking,
            reasoning_effort=body.reasoning_effort,
            stream=body.stream,
            enable_context_manager=body.enable_context_manager,
            agent_config=agent_config,  # Pass agent configuration
            resume_strategy=body.resume_strategy or "auto",
            resume_window_minutes=body.resume_window_minutes or 1440,
            shadow_clone_mode=effective_shadow_clone_mode,
            shadow_clone_main_model=shadow_clone_model_selection[
                "effective_shadow_clone_main_model"
            ],
            shadow_clone_subagent_model=shadow_clone_model_selection[
                "effective_shadow_clone_subagent_model"
            ],
            # is_agent_builder=is_agent_builder,
            # target_agent_id=target_agent_id,
            request_id=request_id,
        )
    except Exception as dispatch_error:
        if run_capacity_reservation_acquired:
            await _release_run_capacity_reservation_best_effort(
                agent_run_id=agent_run_id,
                owner_token=run_capacity_reservation_owner,
            )
        if phase2_initial_attempt_created:
            await _mark_phase2_admission_attempt_failed_best_effort(
                client,
                agent_run_id=agent_run_id,
                error_message=f"Failed to dispatch agent run: {dispatch_error}",
            )
        await update_agent_run_status(
            client,
            agent_run_id,
            "failed",
            error=f"Failed to dispatch agent run: {dispatch_error}",
        )
        raise

    return {"agent_run_id": agent_run_id, "status": "running"}


@router.post("/agent-run/{agent_run_id}/stop")
async def stop_agent(
    agent_run_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Stop a running agent."""
    structlog.contextvars.bind_contextvars(
        agent_run_id=agent_run_id,
    )

    # 1. POST /agent-run/{id}/stop
    # 2. 执行 stop_agent_run() 函数
    # 3. 调用 update_agent_run_status()  更新数据库状态
    # 4. 发布Redis STOP信号  通知Agent停止
    # 5. 调用 _cleanup_redis_response_list()  清理Redis
    # 6. 返回 {"status": "stopped"}

    logger.info(f"Received request to stop agent run: {agent_run_id}")
    client = await db.client
    await get_agent_run_with_access_check(client, agent_run_id, user_id)
    await stop_agent_run(agent_run_id)
    return {"status": "stopped"}


async def _build_agent_run_file_delivery_source(
    agent_run_id: str,
    parent_status: Optional[str],
    *,
    thread_id: Optional[str] = None,
    project_id: Optional[str] = None,
    client: Any = None,
    has_thread_artifacts: Optional[bool] = None,
) -> dict[str, Any]:
    lease = None
    state = None
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if normalized_agent_run_id:
        try:
            lease = await get_run_sandbox_lease(normalized_agent_run_id)
        except Exception:
            logger.warning(
                "Failed to load sandbox lease for agent run file delivery source: %s",
                normalized_agent_run_id,
            )
        if lease is None or not _lease_has_file_delivery_identity(lease):
            try:
                state = await get_state(normalized_agent_run_id)
            except Exception:
                logger.warning(
                    "Failed to load shadow clone state for agent run file delivery source: %s",
                    normalized_agent_run_id,
                )

    source = build_run_file_delivery_source(
        agent_run_id=normalized_agent_run_id,
        parent_status=parent_status,
        lease=lease,
        state=state,
    )

    # Fallback for Claude SDK local mode (no E2B sandbox):
    # when no sandbox lease/state, check if workspace artifacts exist for this run.
    if (
        not source.get("browse_sandbox_id")
        and normalized_agent_run_id
        and await workspace_artifacts.has_artifacts_for_run(normalized_agent_run_id)
    ):
        synthetic_id = f"claude-local:{normalized_agent_run_id}"
        source["browse_sandbox_id"] = synthetic_id
        source["archive_sandbox_id"] = synthetic_id
        source["identity_source"] = "workspace_artifacts"
        source["browse_root_path"] = "/workspace"
        source["archive_root_path"] = "/workspace"

    normalized_thread_id = str(thread_id or "").strip()
    normalized_project_id = str(project_id or "").strip()
    thread_artifacts_available = bool(has_thread_artifacts)
    if has_thread_artifacts is None and normalized_thread_id and normalized_project_id:
        thread_artifacts_available = await workspace_artifacts.has_artifacts_for_thread(
            project_id=normalized_project_id,
            thread_id=normalized_thread_id,
            client=client,
        )

    if normalized_thread_id and normalized_project_id and thread_artifacts_available:
        synthetic_id = f"thread-workspace:{normalized_thread_id}"
        source["agent_run_id"] = f"thread-workspace:{normalized_thread_id}"
        source["browse_sandbox_id"] = synthetic_id
        source["archive_sandbox_id"] = synthetic_id
        source["identity_source"] = "thread_workspace_artifacts"
        source["browse_root_path"] = "/workspace"
        source["archive_root_path"] = "/workspace"
        source["artifact_scope"] = "thread"
        source["thread_id"] = normalized_thread_id

    return source


@router.get("/thread/{thread_id}/agent-runs")
async def get_agent_runs(
    thread_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Get all agent runs for a thread."""
    print(f"🔍 ===== 查询线程Agent运行记录 =====")
    print(f"  📋 thread_id: {thread_id}")
    print(f"  👤 user_id: {user_id}")

    structlog.contextvars.bind_contextvars(
        thread_id=thread_id,
    )
    logger.info(f"Fetching agent runs for thread: {thread_id}")
    client = await db.client
    await verify_thread_access(client, thread_id, user_id)

    thread_project_id: Optional[str] = None
    try:
        thread_result = (
            await client.table("threads")
            .select("project_id")
            .eq("thread_id", thread_id)
            .execute()
        )
        if thread_result.data:
            thread_project_id = (
                str((thread_result.data[0] or {}).get("project_id") or "").strip()
                or None
            )
    except Exception as thread_project_error:
        logger.warning(
            "Failed to load thread project for file delivery source: %s",
            thread_project_error,
        )

    thread_has_artifacts: Optional[bool] = None
    if thread_project_id:
        thread_has_artifacts = await workspace_artifacts.has_artifacts_for_thread(
            project_id=thread_project_id,
            thread_id=thread_id,
            client=client,
        )

    agent_runs = (
        await client.table("agent_runs")
        .select(
            "id, agent_run_id, thread_id, status, started_at, completed_at, error, created_at, updated_at, metadata"
        )
        .eq("thread_id", thread_id)
        .order("created_at", desc=True)
        .execute()
    )

    for i, run in enumerate(agent_runs.data):
        logger.info(
            f"    {i+1}. ID: {run.get('id')}, agent_run_id: {run.get('agent_run_id')}, status: {run.get('status')}, started_at: {run.get('started_at')}, completed_at: {run.get('completed_at')}"
        )

    # 处理返回数据，确保使用正确的ID字段
    projected_runs = await asyncio.gather(
        *(
            _project_agent_run_row_terminal_status(dict(run), client=client)
            for run in (agent_runs.data or [])
        )
    )

    processed_runs = []
    for run in projected_runs:
        processed_run = dict(run)
        effective_agent_run_id = str(
            processed_run.get("agent_run_id") or processed_run.get("id") or ""
        ).strip()
        # 优先使用agent_run_id，如果没有则使用id
        if processed_run.get("agent_run_id"):
            processed_run["id"] = processed_run["agent_run_id"]
        processed_run["file_delivery_source"] = (
            await _build_agent_run_file_delivery_source(
                effective_agent_run_id,
                processed_run.get("status"),
                thread_id=thread_id,
                project_id=(
                    thread_project_id
                    or (
                        processed_run.get("metadata", {})
                        if isinstance(processed_run.get("metadata"), dict)
                        else {}
                    ).get("project_id")
                ),
                client=client,
                has_thread_artifacts=thread_has_artifacts,
            )
        )
        processed_runs.append(processed_run)

    logger.debug(f"Found {len(agent_runs.data)} agent runs for thread: {thread_id}")

    return {"agent_runs": processed_runs}


@router.get("/agent-run/{agent_run_id}")
async def get_agent_run(
    agent_run_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Get agent run status and responses."""
    structlog.contextvars.bind_contextvars(
        agent_run_id=agent_run_id,
    )
    logger.info(f"Fetching agent run details: {agent_run_id}")
    client = await db.client
    agent_run_data = await get_agent_run_with_access_check(
        client, agent_run_id, user_id
    )
    agent_run_data = await _project_agent_run_row_terminal_status(
        dict(agent_run_data),
        client=client,
    )
    file_delivery_source = await _build_agent_run_file_delivery_source(
        agent_run_data.get("agent_run_id", agent_run_id),
        agent_run_data.get("status"),
    )
    # Note: Responses are not included here by default, they are in the stream or DB
    return {
        "id": agent_run_data["id"],
        "threadId": agent_run_data["thread_id"],
        "status": agent_run_data["status"],
        "startedAt": agent_run_data["started_at"],
        "completedAt": agent_run_data["completed_at"],
        "error": agent_run_data["error"],
        "file_delivery_source": file_delivery_source,
    }


@router.get("/thread/{thread_id}/agent", response_model=ThreadAgentResponse)
async def get_thread_agent(
    thread_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Get the agent details for a specific thread. Since threads are fully agent-agnostic,
    this returns the most recently used agent from agent_runs only."""
    structlog.contextvars.bind_contextvars(
        thread_id=thread_id,
    )
    logger.info(f"Fetching agent details for thread: {thread_id}")
    client = await db.client

    try:
        # Verify thread access and get thread data
        await verify_thread_access(client, thread_id, user_id)
        thread_result = (
            await client.table("threads")
            .select("account_id")
            .eq("thread_id", thread_id)
            .execute()
        )

        if not thread_result.data:
            raise HTTPException(status_code=404, detail="Thread not found")

        thread_data = thread_result.data[0]
        account_id = thread_data.get("account_id")

        effective_agent_id = None
        agent_source = "none"

        # Get the most recently used agent from agent_runs
        recent_agent_result = (
            await client.table("agent_runs")
            .select("agent_id", "agent_version_id")
            .eq("thread_id", thread_id)
            .neq("agent_id", None)
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if recent_agent_result.data:
            effective_agent_id = recent_agent_result.data[0]["agent_id"]
            recent_version_id = recent_agent_result.data[0].get("agent_version_id")
            agent_source = "recent"
            logger.info(
                f"Found most recently used agent: {effective_agent_id} (version: {recent_version_id})"
            )

        # If no agent found in agent_runs
        if not effective_agent_id:
            return {
                "agent": None,
                "source": "none",
                "message": "No agent has been used in this thread yet. Threads are agent-agnostic - use /agent/start to select an agent.",
            }

        # Fetch the agent details
        agent_result = (
            await client.table("agents")
            .select("*")
            .eq("agent_id", effective_agent_id)
            .eq("user_id", user_id)
            .execute()
        )

        if not agent_result.data:
            # Agent was deleted or doesn't exist
            return {
                "agent": None,
                "source": "missing",
                "message": f"Agent {effective_agent_id} not found or was deleted. You can select a different agent.",
            }

        agent_data = agent_result.data[0]

        # Use versioning system to get current version data
        version_data = None
        current_version = None
        if agent_data.get("current_version_id"):
            try:
                version_service = await _get_version_service()
                current_version_obj = await version_service.get_version(
                    agent_id=effective_agent_id,
                    version_id=agent_data["current_version_id"],
                    user_id=user_id,
                )
                current_version_data = current_version_obj.to_dict()
                version_data = current_version_data

                # Create AgentVersionResponse from version data
                current_version = AgentVersionResponse(
                    version_id=current_version_data["version_id"],
                    agent_id=current_version_data["agent_id"],
                    version_number=current_version_data["version_number"],
                    version_name=current_version_data["version_name"],
                    system_prompt=current_version_data["system_prompt"],
                    model=current_version_data.get("model"),
                    configured_mcps=current_version_data.get("configured_mcps", []),
                    custom_mcps=current_version_data.get("custom_mcps", []),
                    agentpress_tools=current_version_data.get("agentpress_tools", {}),
                    is_active=current_version_data.get("is_active", True),
                    created_at=current_version_data["created_at"],
                    updated_at=current_version_data.get(
                        "updated_at", current_version_data["created_at"]
                    ),
                    created_by=current_version_data.get("created_by"),
                )

                logger.info(
                    f"Using agent {agent_data['name']} version {current_version_data.get('version_name', 'v1')}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to get version data for agent {effective_agent_id}: {e}"
                )

        version_data = None
        if current_version:
            version_data = {
                "version_id": current_version.version_id,
                "agent_id": current_version.agent_id,
                "version_number": current_version.version_number,
                "version_name": current_version.version_name,
                "system_prompt": current_version.system_prompt,
                "model": current_version.model,
                "configured_mcps": current_version.configured_mcps,
                "custom_mcps": current_version.custom_mcps,
                "agentpress_tools": current_version.agentpress_tools,
                "is_active": current_version.is_active,
                "created_at": current_version.created_at,
                "updated_at": current_version.updated_at,
                "created_by": current_version.created_by,
            }

        from agent.config_helper import extract_agent_config

        agent_config = extract_agent_config(agent_data, version_data)

        system_prompt = agent_config["system_prompt"]
        configured_mcps = agent_config["configured_mcps"]
        custom_mcps = agent_config["custom_mcps"]
        agentpress_tools = agent_config["agentpress_tools"]

        return {
            "agent": AgentResponse(
                agent_id=agent_data["agent_id"],
                account_id=user_id,  # 使用 user_id 作为 account_id
                name=agent_data["name"],
                description=agent_data.get("description"),
                system_prompt=system_prompt,
                configured_mcps=configured_mcps,
                custom_mcps=custom_mcps,
                agentpress_tools=agentpress_tools,
                is_default=agent_data.get("is_default", False),
                is_public=agent_data.get("is_public", False),
                tags=agent_data.get("tags", []),
                avatar=agent_config.get("avatar"),
                avatar_color=agent_config.get("avatar_color"),
                profile_image_url=agent_config.get("profile_image_url"),
                created_at=(
                    agent_data["created_at"].isoformat()
                    if isinstance(agent_data["created_at"], datetime)
                    else str(agent_data["created_at"])
                ),
                updated_at=(
                    agent_data.get("updated_at", agent_data["created_at"]).isoformat()
                    if isinstance(
                        agent_data.get("updated_at", agent_data["created_at"]), datetime
                    )
                    else str(agent_data.get("updated_at", agent_data["created_at"]))
                ),
                current_version_id=agent_data.get("current_version_id"),
                version_count=agent_data.get("version_count", 1),
                current_version=current_version,
                metadata=(
                    json.loads(agent_data.get("metadata", "{}"))
                    if isinstance(agent_data.get("metadata"), str)
                    else agent_data.get("metadata", {})
                ),
            ),
            "source": agent_source,
            "message": f"Using {agent_source} agent: {agent_data['name']}. Threads are agent-agnostic - you can change agents anytime.",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching agent for thread {thread_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch thread agent: {str(e)}"
        )


@router.get("/agent-run/{agent_run_id}/stream")
async def stream_agent_run(
    agent_run_id: str,
    token: Optional[str] = None,
    request: Request = None,
    from_index: int = Query(0, ge=0),
    from_event_id: Optional[str] = Query(None),
):
    """Stream the responses of an agent run using Redis Lists and Pub/Sub.

    示例：
    12:00:00.100 - 前端: POST /agent/initiate
    12:00:00.200 - 后端: run_agent_background.send() 任务入队
    12:00:00.300 - 后端: 立即返回 {thread_id, agent_run_id}
    12:00:00.400 - 前端: 收到响应，立即发起 GET /stream

    # === 并行执行 ===
    12:00:00.500 - Worker: 开始执行 run_agent_background  Agent开始运行
    12:00:00.600 - Worker: async for response in agent_gen:  开始迭代响应
    12:00:00.700 - Worker: 第一个response存入Redis
    12:00:00.800 - Stream: 从Redis读取到第一个response，推送给前端

    12:00:01.000 - Worker: 第二个response存入Redis
    12:00:01.100 - Stream: 实时推送第二个response给前端
    """
    logger.info(f"[SSE] Starting stream for agent run: {agent_run_id}")
    client = await db.client
    # 进行身份校验
    user_id = await get_user_id_from_stream_auth(request, token)  # 瞬时验证
    logger.info(f"[SSE] user authenticated successfully: {user_id}")
    # 检查agent_run访问权限
    agent_run_data = await get_agent_run_with_access_check(
        client, agent_run_id, user_id
    )  # 1 db query
    agent_run_data = dict(agent_run_data)
    is_shadow_clone_v2_stream = _agent_run_uses_shadow_clone_v2_event_stream(
        agent_run_data
    )
    if not is_shadow_clone_v2_stream:
        agent_run_data = await _project_agent_run_row_terminal_status(
            agent_run_data,
            client=client,
        )
    logger.info(
        f"[SSE] agent_run data fetched, status: {agent_run_data.get('status') if agent_run_data else 'None'}"
    )

    STREAM_PING_INTERVAL = AGENT_STREAM_PING_INTERVAL_SECONDS
    STREAM_POLL_INTERVAL = AGENT_STREAM_QUEUE_POLL_INTERVAL_SECONDS
    PUBSUB_POLL_INTERVAL = AGENT_STREAM_PUBSUB_POLL_INTERVAL_SECONDS

    if is_shadow_clone_v2_stream:

        async def v2_event_stream_generator():
            requested_v2_from_index = max(0, int(from_index or 0))
            requested_v2_from_event_id = str(from_event_id or "").strip()
            emitted_event_ids: set[str] = set()
            emitted_assistant_response_ids: set[str] = set()
            terminal_seen = False
            emitted_any = False

            async def _emit_available_events() -> list[str]:
                nonlocal terminal_seen, emitted_any
                events = await shadow_clone_v2_event_log.read_events(
                    agent_run_id,
                    start="-",
                )
                append_order_event_ids = [_v2_event_id(event) for event in events]
                cursor_position: int | None = None
                cursor_was_found = False
                replay_event_ids_after_cursor: set[str] | None = None
                if requested_v2_from_event_id:
                    if requested_v2_from_event_id in append_order_event_ids:
                        cursor_was_found = True
                        cursor_position = append_order_event_ids.index(
                            requested_v2_from_event_id
                        )
                        replay_event_ids_after_cursor = set(
                            event_id
                            for event_id in append_order_event_ids[
                                cursor_position + 1 :
                            ]
                            if event_id
                        )
                ordered_events = [
                    event for _idx, event in _order_v2_stream_events(events)
                ]
                append_prefix_by_event_id: dict[str, list[Any]] = {}
                append_prefix: list[Any] = []
                for event in events:
                    append_prefix.append(event)
                    event_id = _v2_event_id(event)
                    if event_id:
                        append_prefix_by_event_id[event_id] = list(append_prefix)
                payloads: list[str] = []
                if not ordered_events:
                    return payloads

                assistant_responses_cache: list[dict[str, Any]] | None = None

                async def _append_user_facing_assistant_payloads() -> None:
                    nonlocal assistant_responses_cache
                    if assistant_responses_cache is None:
                        assistant_responses_cache = (
                            await _read_shadow_clone_v2_user_facing_assistant_stream_responses(
                                agent_run_id
                            )
                        )
                    for response in assistant_responses_cache:
                        response_id = _shadow_clone_v2_assistant_response_identity(
                            response
                        )
                        if response_id in emitted_assistant_response_ids:
                            continue
                        payloads.append(
                            f"data: {json.dumps(response, ensure_ascii=False)}\n\n"
                        )
                        emitted_assistant_response_ids.add(response_id)

                first_retained_event = ordered_events[0]
                retained_history_is_complete = _is_run_started_v2_event(
                    first_retained_event
                )
                if not retained_history_is_complete:
                    terminal_seen = True
                    emitted_any = True
                    unavailable_payload = {
                        "type": "replay_unavailable",
                        "status": "error",
                        "event_index": requested_v2_from_index,
                        "message": (
                            "Shadow Clone V2 event replay is unavailable for the "
                            "requested cursor."
                        ),
                        "metadata": {"stream_source": "shadow_clone_v2_event_log"},
                    }
                    return [
                        "data: "
                        f"{json.dumps(unavailable_payload, ensure_ascii=False)}\n\n"
                    ]

                terminal_prefix: list[Any] = []
                latest_terminal_event: Any = None
                prefix_candidate: list[Any] = []
                for event in ordered_events:
                    prefix_candidate.append(event)
                    if _is_terminal_v2_event(event):
                        latest_terminal_event = event
                        terminal_prefix = list(prefix_candidate)

                if latest_terminal_event is not None and requested_v2_from_event_id:
                    latest_terminal_event_id = _v2_event_id(latest_terminal_event)
                    latest_terminal_append_position = (
                        append_order_event_ids.index(latest_terminal_event_id)
                        if latest_terminal_event_id in append_order_event_ids
                        else None
                    )
                    cursor_at_or_after_terminal = (
                        cursor_was_found
                        and cursor_position is not None
                        and latest_terminal_append_position is not None
                        and cursor_position >= latest_terminal_append_position
                    )
                    cursor_missing = not cursor_was_found
                    no_replay_events_after_cursor = (
                        replay_event_ids_after_cursor is not None
                        and not replay_event_ids_after_cursor
                    )
                    if cursor_missing or (
                        no_replay_events_after_cursor and cursor_at_or_after_terminal
                    ):
                        await _append_user_facing_assistant_payloads()
                        terminal_payload = _build_v2_projection_stream_payload(
                            agent_run_id=agent_run_id,
                            events_prefix=terminal_prefix,
                            event=latest_terminal_event,
                            parent_status=agent_run_data.get("status"),
                        )
                        terminal_payload["metadata"]["terminal_summary"] = True
                        terminal_payload["metadata"][
                            "requested_from_index"
                        ] = requested_v2_from_index
                        terminal_payload["metadata"]["requested_from_event_id"] = (
                            requested_v2_from_event_id
                        )
                        terminal_payload["metadata"]["event_cursor_found"] = (
                            cursor_was_found
                        )
                        terminal_payload["metadata"]["latest_terminal_sequence"] = (
                            _v2_event_sequence(latest_terminal_event)
                        )
                        terminal_seen = True
                        emitted_any = True
                        emitted_event_ids.add(_v2_event_id(latest_terminal_event))
                        payloads.append(
                            "data: "
                            f"{json.dumps(terminal_payload, ensure_ascii=False)}\n\n"
                        )
                        return payloads
                if (
                    latest_terminal_event is not None
                    and not requested_v2_from_event_id
                    and requested_v2_from_index
                    > _v2_event_sequence(latest_terminal_event)
                ):
                    await _append_user_facing_assistant_payloads()
                    terminal_payload = _build_v2_projection_stream_payload(
                        agent_run_id=agent_run_id,
                        events_prefix=terminal_prefix,
                        event=latest_terminal_event,
                        parent_status=agent_run_data.get("status"),
                    )
                    terminal_payload["metadata"]["terminal_summary"] = True
                    terminal_payload["metadata"][
                        "requested_from_index"
                    ] = requested_v2_from_index
                    terminal_payload["metadata"]["latest_terminal_sequence"] = (
                        _v2_event_sequence(latest_terminal_event)
                    )
                    terminal_seen = True
                    emitted_any = True
                    emitted_event_ids.add(_v2_event_id(latest_terminal_event))
                    payloads.append(
                        "data: "
                        f"{json.dumps(terminal_payload, ensure_ascii=False)}\n\n"
                    )
                    return payloads

                prefix: list[Any] = []
                emitted_projection_this_poll = False
                for event in ordered_events:
                    prefix.append(event)
                    sequence = _v2_event_sequence(event)
                    event_id = _v2_event_id(event)
                    if event_id in emitted_event_ids:
                        continue
                    if replay_event_ids_after_cursor is not None:
                        if event_id not in replay_event_ids_after_cursor:
                            continue
                    elif sequence < requested_v2_from_index:
                        continue
                    payload = _build_v2_projection_stream_payload(
                        agent_run_id=agent_run_id,
                        events_prefix=append_prefix_by_event_id.get(
                            event_id,
                            list(prefix),
                        ),
                        event=event,
                        parent_status=agent_run_data.get("status"),
                    )
                    payloads.append(
                        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    )
                    emitted_event_ids.add(event_id)
                    emitted_any = True
                    emitted_projection_this_poll = True
                    await _append_user_facing_assistant_payloads()
                    if _is_terminal_v2_event(event):
                        terminal_seen = True
                        break
                if not emitted_projection_this_poll:
                    await _append_user_facing_assistant_payloads()
                return payloads

            try:
                for payload in await _emit_available_events():
                    yield payload
                if terminal_seen:
                    return

                last_ping_time = time.monotonic()
                while True:
                    if request is not None and await request.is_disconnected():
                        return
                    for payload in await _emit_available_events():
                        yield payload
                    if terminal_seen:
                        return
                    now = time.monotonic()
                    if now - last_ping_time >= STREAM_PING_INTERVAL:
                        yield f"data: {json.dumps({'type': 'ping', 'non_progress': True})}\n\n"
                        last_ping_time = now
                    await asyncio.sleep(STREAM_POLL_INTERVAL)
            except Exception as error:
                logger.error(
                    "[SSE] V2 event-log stream failed for %s: %s",
                    agent_run_id,
                    error,
                    exc_info=True,
                )
                yield f"data: {json.dumps({'type': 'status', 'status': 'error', 'message': 'Shadow Clone V2 stream failed.'}, ensure_ascii=False)}\n\n"

        return StreamingResponse(
            v2_event_stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Content-Type": "text/event-stream",
            },
        )

    # 结构化日志上下文，将 agent_run_id 和 user_id 绑定到当前请求的上下文中，后续的所有日志记录都会自动包含这些信息
    structlog.contextvars.bind_contextvars(
        agent_run_id=agent_run_id,
        user_id=user_id,
    )
    # 定义Redis中的键名，用于流式输出的数据存储和通信
    response_channel = f"agent_run:{agent_run_id}:new_response"  # Redis Pub/Sub 频道名，用于通知新响应到达
    control_channel = f"agent_run:{agent_run_id}:control"  # edis Pub/Sub 频道名，用于控制信号，比如发送停止、暂停、错误、管理流式输出的生命周期

    requested_from_index = max(0, int(from_index or 0))
    initial_from_index = requested_from_index

    async def _fetch_responses(
        response_list_key: str,
        start_index: int,
        *,
        current_execution_epoch: Optional[int],
    ):
        raw_items = await redis.lrange(response_list_key, start_index, -1)
        parsed_items = []
        next_index = start_index
        for raw in raw_items:
            try:
                parsed = json.loads(raw)
            except Exception:
                next_index += 1
                continue
            if not isinstance(parsed, dict):
                next_index += 1
                continue
            parsed["event_index"] = _build_public_stream_event_index(
                local_event_index=next_index,
                current_execution_epoch=current_execution_epoch,
            )
            parsed_items.append(parsed)
            next_index += 1
        return parsed_items

    async def stream_generator(agent_run_data):
        # 定义流式生成器元数据
        last_processed_index = initial_from_index - 1
        pubsub_response = None
        pubsub_control = None
        listener_task = None
        terminate_stream = False
        initial_yield_complete = False
        pending_new_response_signal = False
        current_execution_epoch: Optional[int] = None
        phase2_public_stream = (
            _read_regular_execution_mode_from_metadata(agent_run_data.get("metadata"))
            == "phase2_supervisor"
        )
        current_response_list_key: Optional[str] = (
            None if phase2_public_stream else build_response_list_key(agent_run_id)
        )
        current_status = agent_run_data.get("status") if agent_run_data else None
        authoritative_terminal_status = _resolve_stream_terminal_status(current_status)

        async def _refresh_response_source(
            *,
            reset_cursor_on_switch: bool,
        ) -> tuple[Optional[str], bool]:
            nonlocal current_execution_epoch, current_response_list_key
            nonlocal last_processed_index

            (
                resolved_execution_epoch,
                resolved_response_list_key,
                lookup_succeeded,
            ) = await _resolve_public_response_stream_source(
                client,
                agent_run_id=agent_run_id,
                agent_run_row=agent_run_data,
            )
            if phase2_public_stream and not lookup_succeeded:
                return current_response_list_key, False
            if phase2_public_stream and not resolved_response_list_key:
                return current_response_list_key, False
            switched = resolved_response_list_key != current_response_list_key
            if switched:
                logger.info(
                    "[SSE] Switching response source for %s: previous_epoch=%s "
                    "new_epoch=%s previous_key=%s new_key=%s",
                    agent_run_id,
                    current_execution_epoch,
                    resolved_execution_epoch,
                    current_response_list_key,
                    resolved_response_list_key,
                )
                current_execution_epoch = resolved_execution_epoch
                current_response_list_key = resolved_response_list_key
                if reset_cursor_on_switch:
                    last_processed_index = -1
            else:
                current_execution_epoch = resolved_execution_epoch
            return current_response_list_key, switched

        def _is_terminal_status_response(response: Dict[str, Any]) -> bool:
            return response.get("type") == "status" and response.get("status") in {
                "completed",
                "failed",
                "stopped",
            }

        def _log_if_write_file_chunk(response: Dict[str, Any], phase: str) -> None:
            try:
                if response.get("type") != "assistant":
                    return
                metadata = response.get("metadata")
                if isinstance(metadata, str):
                    metadata = json.loads(metadata)
                if not isinstance(metadata, dict):
                    return
                if metadata.get("stream_status") != "tool_call_chunk":
                    return
                tool_calls = metadata.get("tool_calls") or []
                tool_name = None
                if tool_calls and isinstance(tool_calls, list):
                    tool_name = tool_calls[0].get("function", {}).get("name")
                if tool_name == "write_file":
                    logger.info(
                        "[SSE] %s write_file tool_call_chunk sent to client for %s",
                        phase,
                        agent_run_id,
                    )
            except Exception:
                # Keep stream path best-effort: debug logging must never break delivery.
                pass

        def _payloads_include_terminal_status(
            payloads: List[str],
            *,
            status_value: Optional[str],
        ) -> bool:
            if not status_value:
                return False
            for payload in payloads:
                if not isinstance(payload, str) or not payload.startswith("data: "):
                    continue
                try:
                    parsed_payload = json.loads(payload[6:])
                except Exception:
                    continue
                if (
                    isinstance(parsed_payload, dict)
                    and parsed_payload.get("type") == "status"
                    and parsed_payload.get("status") == status_value
                ):
                    return True
            return False

        async def _resolve_latest_authoritative_terminal_truth() -> (
            tuple[Optional[str], Optional[str]]
        ):
            try:
                refreshed_agent_run_data = await get_agent_run_with_access_check(
                    client,
                    agent_run_id,
                    user_id,
                )
                refreshed_agent_run_data = await _project_agent_run_row_terminal_status(
                    dict(refreshed_agent_run_data),
                    client=client,
                )
            except Exception as refresh_error:
                logger.warning(
                    "[SSE] Failed to refresh authoritative terminal truth for %s: %s",
                    agent_run_id,
                    refresh_error,
                )
            else:
                refreshed_terminal_status = _resolve_stream_terminal_status(
                    refreshed_agent_run_data.get("status")
                )
                if refreshed_terminal_status:
                    return refreshed_terminal_status, refreshed_agent_run_data.get(
                        "error"
                    )

            try:
                (
                    resolved_execution_epoch,
                    _response_list_key,
                    lookup_succeeded,
                ) = await _resolve_public_response_stream_source(
                    client,
                    agent_run_id=agent_run_id,
                    agent_run_row=agent_run_data,
                )
                if phase2_public_stream and not lookup_succeeded:
                    if current_execution_epoch is None:
                        return None, None
                    resolved_execution_epoch = current_execution_epoch
                if phase2_public_stream and resolved_execution_epoch is None:
                    return None, None
                return await _read_projected_terminal_status_from_response_tail(
                    agent_run_id,
                    current_execution_epoch=resolved_execution_epoch,
                )
            except Exception as projection_error:
                logger.warning(
                    "[SSE] Failed to project terminal truth from response tail for %s: %s",
                    agent_run_id,
                    projection_error,
                )
                return None, None

        async def _collect_response_payloads(
            start_index: int,
            phase: str,
            *,
            authoritative_terminal_status: Optional[str] = None,
            allow_epoch_switch_reset: bool = True,
        ) -> List[str]:
            nonlocal last_processed_index, terminate_stream

            response_list_key, source_switched = await _refresh_response_source(
                reset_cursor_on_switch=allow_epoch_switch_reset,
            )
            if source_switched and allow_epoch_switch_reset:
                start_index = 0
            if not response_list_key:
                return []

            effective_start_index = _resolve_response_fetch_start_index(
                requested_from_index=start_index,
                current_execution_epoch=current_execution_epoch,
            )
            backlog_limit: Optional[int] = None
            if agent_run_data.get("status") == "running" and not phase.startswith(
                "drain:"
            ):
                if phase == "initial" and effective_start_index == 0:
                    backlog_limit = AGENT_STREAM_INITIAL_REPLAY_LIMIT
                else:
                    backlog_limit = AGENT_STREAM_RUNNING_CATCHUP_LIMIT

            if backlog_limit is not None:
                try:
                    response_count = int(await redis.llen(response_list_key))
                except Exception as error:
                    logger.warning(
                        "[SSE] Failed to inspect running backlog for %s phase=%s start_index=%s: %s",
                        agent_run_id,
                        phase,
                        start_index,
                        error,
                    )
                else:
                    pending = max(0, response_count - effective_start_index)
                    if pending > backlog_limit:
                        effective_start_index = max(
                            effective_start_index,
                            response_count
                            - (
                                backlog_limit * AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER
                            ),
                        )
                        logger.warning(
                            "[SSE] Priority backlog scan activated for %s phase=%s start_index=%s effective_start_index=%s pending=%s limit=%s multiplier=%s",
                            agent_run_id,
                            phase,
                            start_index,
                            effective_start_index,
                            pending,
                            backlog_limit,
                            AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER,
                        )

            responses = await _fetch_responses(
                response_list_key,
                effective_start_index,
                current_execution_epoch=current_execution_epoch,
            )
            if (
                not responses
                and phase2_public_stream
                and phase == "initial"
                and effective_start_index == 0
                and _is_agent_run_terminal_status(agent_run_data.get("status"))
                and current_execution_epoch is not None
            ):
                (
                    fallback_execution_epoch,
                    fallback_response_list_key,
                ) = await _resolve_phase2_terminal_replay_fallback_source(
                    client,
                    agent_run_id=agent_run_id,
                    current_execution_epoch=current_execution_epoch,
                )
                if (
                    fallback_response_list_key is not None
                    and fallback_execution_epoch is not None
                ):
                    logger.info(
                        "[SSE] Falling back to previous terminal replay source for %s latest_epoch=%s fallback_epoch=%s key=%s",
                        agent_run_id,
                        current_execution_epoch,
                        fallback_execution_epoch,
                        fallback_response_list_key,
                    )
                    responses = await _fetch_responses(
                        fallback_response_list_key,
                        0,
                        current_execution_epoch=fallback_execution_epoch,
                    )
            if not responses:
                return []

            selected_responses = responses
            if backlog_limit is not None and len(responses) > backlog_limit:
                selected_responses = _select_priority_backlog_window(
                    responses,
                    limit=backlog_limit,
                )
                logger.warning(
                    "[SSE] Priority backlog trim applied for %s phase=%s fetched=%s kept=%s dropped=%s productive_kept=%s non_progress_kept=%s",
                    agent_run_id,
                    phase,
                    len(responses),
                    len(selected_responses),
                    len(responses) - len(selected_responses),
                    sum(
                        1
                        for response in selected_responses
                        if not _is_non_progress_stream_response(response)
                    ),
                    sum(
                        1
                        for response in selected_responses
                        if _is_non_progress_stream_response(response)
                    ),
                )

            payloads: List[str] = []
            for response in selected_responses:
                if _should_filter_replayed_terminal_status(
                    response,
                    authoritative_terminal_status=authoritative_terminal_status,
                ):
                    logger.info(
                        "[SSE] Skipping replayed terminal status for %s phase=%s authoritative_terminal_status=%s replay_status=%s",
                        agent_run_id,
                        phase,
                        authoritative_terminal_status,
                        response.get("status"),
                    )
                    last_processed_index = int(
                        response.get("event_index", last_processed_index),
                    )
                    continue
                _log_if_write_file_chunk(response, phase)
                payloads.append(
                    f"data: {json.dumps(_decorate_stream_response(response))}\n\n"
                )
                last_processed_index = int(
                    response.get("event_index", last_processed_index),
                )
                if _is_terminal_status_response(response):
                    logger.info(
                        "[SSE] Detected terminal status via %s: %s",
                        phase,
                        response.get("status"),
                    )
                    terminate_stream = True
                    break

            logger.info(
                "[SSE] %s fetch complete: sent=%s last_processed_index=%s start_index=%s effective_start_index=%s",
                phase,
                len(payloads),
                last_processed_index,
                start_index,
                effective_start_index,
            )
            return payloads

        async def _drain_pending_payloads(
            reason: str,
            *,
            authoritative_terminal_status: Optional[str] = None,
        ) -> List[str]:
            start_index = last_processed_index + 1
            payloads = await _collect_response_payloads(
                start_index,
                phase=f"drain:{reason}",
                authoritative_terminal_status=authoritative_terminal_status,
            )
            if payloads:
                logger.info(
                    "[SSE] Drain completed for %s: delivered=%s pending responses",
                    reason,
                    len(payloads),
                )
            return payloads

        try:
            # 1. 捕获 Redis List 中的初始响应，并发送给前端
            initial_payloads = await _collect_response_payloads(
                initial_from_index,
                phase="initial",
                authoritative_terminal_status=authoritative_terminal_status,
                allow_epoch_switch_reset=False,
            )
            logger.info(
                "[SSE] Initial fetch from Redis: %s responses for %s from index %s",
                len(initial_payloads),
                agent_run_id,
                initial_from_index,
            )
            if initial_payloads:
                for payload in initial_payloads:
                    yield payload
            else:
                logger.info(
                    f"[SSE] No initial responses found in Redis for {agent_run_id}"
                )

            initial_yield_complete = True

            # 2. 状态检查
            # 目的：避免对已完成的agent_run进行不必要的监听
            # Preserve the backend terminal status for late subscribers.
            if current_status != "running":
                logger.info(
                    f"Agent run {agent_run_id} is not running (status: {current_status}). Ending stream."
                )
                terminal_status = authoritative_terminal_status or "completed"
                terminal_message = _build_terminal_stream_status_payload(
                    terminal_status,
                    message=agent_run_data.get("error"),
                )
                yield f"data: {json.dumps(terminal_message)}\n\n"
                return
            # 下面是 Agent 处于 Running 状态时的流程
            structlog.contextvars.bind_contextvars(
                thread_id=agent_run_data.get("thread_id"),
            )

            # 3. 设置 Pub/Sub 监听器，用于接收新响应和控制信号
            # 目的：建立实时监听，监听 Redis 中的新响应和控制信号，并将其传递给流式生成器
            # 创建两个独立的 Pub/Sub 客户端
            pubsub_response_task = asyncio.create_task(
                redis.create_pubsub()
            )  # 监听Agent产生的新响应
            pubsub_control_task = asyncio.create_task(
                redis.create_pubsub()
            )  # 监听用户的控制信号（停止等）

            pubsub_response, pubsub_control = await asyncio.gather(
                pubsub_response_task, pubsub_control_task
            )

            # 订阅频道并发执行
            response_subscribe_task = asyncio.create_task(
                pubsub_response.subscribe(response_channel)
            )
            control_subscribe_task = asyncio.create_task(
                pubsub_control.subscribe(control_channel)
            )

            await asyncio.gather(response_subscribe_task, control_subscribe_task)

            # 创建消息队列，用于在监听器和主生成器循环之间通信
            message_queue = asyncio.Queue()

            async def _enqueue_new_response_signal() -> None:
                nonlocal pending_new_response_signal
                if pending_new_response_signal:
                    return
                pending_new_response_signal = True
                await message_queue.put({"type": "new_response"})

            # 消息处理循环
            async def listen_messages():
                try:
                    while not terminate_stream:
                        control_message = await pubsub_control.get_message(
                            ignore_subscribe_messages=True,
                            timeout=0.0,
                        )
                        if (
                            control_message
                            and isinstance(control_message, dict)
                            and control_message.get("type") == "message"
                        ):
                            channel = control_message.get("channel")
                            data = control_message.get("data")
                            if isinstance(channel, bytes):
                                channel = channel.decode("utf-8")
                            if isinstance(data, bytes):
                                data = data.decode("utf-8")
                            if channel == control_channel and data in [
                                "STOP",
                                "END_STREAM",
                                "ERROR",
                            ]:
                                await message_queue.put(
                                    {"type": "control", "data": data}
                                )
                                return

                        response_message = await pubsub_response.get_message(
                            ignore_subscribe_messages=True,
                            timeout=PUBSUB_POLL_INTERVAL,
                        )
                        if (
                            response_message
                            and isinstance(response_message, dict)
                            and response_message.get("type") == "message"
                        ):
                            channel = response_message.get("channel")
                            data = response_message.get("data")
                            if isinstance(channel, bytes):
                                channel = channel.decode("utf-8")
                            if isinstance(data, bytes):
                                data = data.decode("utf-8")
                            if channel == response_channel and data == "new":
                                await _enqueue_new_response_signal()
                except asyncio.CancelledError:
                    logger.info(f"Listener task cancelled for {agent_run_id}")
                except Exception as e:
                    logger.error(f"Error in listener for {agent_run_id}: {e}")
                    await message_queue.put(
                        {"type": "error", "data": "Listener failed"}
                    )

            listener_task = asyncio.create_task(listen_messages())
            logger.info(f"Listener task created successfully")

            # 主要的流式输出循环
            last_ping_time = time.monotonic()
            while not terminate_stream:
                if request is not None and await request.is_disconnected():
                    logger.info(
                        f"[SSE] Client disconnected for {agent_run_id}, terminating stream"
                    )
                    terminate_stream = True
                    break
                try:
                    # 等待队列消息（超时则轮询Redis，避免漏掉Pub/Sub消息）
                    try:
                        queue_item = await asyncio.wait_for(
                            message_queue.get(),
                            timeout=STREAM_POLL_INTERVAL,
                        )
                    except asyncio.TimeoutError:
                        # Fallback poll in case pub/sub missed a notification
                        new_start_index = last_processed_index + 1
                        new_payloads = await _collect_response_payloads(
                            new_start_index,
                            phase="poll",
                        )
                        if new_payloads:
                            for payload in new_payloads:
                                yield payload
                            if terminate_stream:
                                logger.info(f"Stream terminated")
                                break
                            continue

                        now = time.monotonic()
                        if now - last_ping_time >= STREAM_PING_INTERVAL:
                            ping_message = {"type": "ping", "non_progress": True}
                            yield f"data: {json.dumps(ping_message)}\n\n"
                            last_ping_time = now
                        continue
                    if queue_item["type"] == "new_response":
                        pending_new_response_signal = False
                        # 获取新响应并发送给前端
                        new_start_index = last_processed_index + 1
                        new_payloads = await _collect_response_payloads(
                            new_start_index,
                            phase="pubsub",
                        )

                        if new_payloads:
                            for payload in new_payloads:
                                yield payload
                        else:
                            logger.info(f"No new responses found")
                        if terminate_stream:
                            logger.info(f"Stream terminated")
                            break

                    elif queue_item["type"] == "control":
                        control_signal = queue_item["data"]
                        terminate_stream = True  # 流式输出终止
                        terminal_status = _resolve_stream_control_terminal_status(
                            control_signal
                        )
                        drained_payloads = await _drain_pending_payloads(
                            reason=f"control:{control_signal}",
                            authoritative_terminal_status=terminal_status,
                        )
                        for payload in drained_payloads:
                            yield payload
                        control_message = _build_terminal_stream_status_payload(
                            terminal_status or "failed",
                            control_signal=control_signal,
                        )
                        yield f"data: {json.dumps(control_message)}\n\n"
                        break

                    elif queue_item["type"] == "error":
                        logger.error(
                            f"Listener error for {agent_run_id}: {queue_item['data']}"
                        )
                        terminate_stream = True
                        (
                            authoritative_terminal_status,
                            authoritative_terminal_message,
                        ) = await _resolve_latest_authoritative_terminal_truth()
                        drained_payloads = await _drain_pending_payloads(
                            reason="listener_error",
                            authoritative_terminal_status=authoritative_terminal_status,
                        )
                        for payload in drained_payloads:
                            yield payload
                        if authoritative_terminal_status:
                            if not _payloads_include_terminal_status(
                                drained_payloads,
                                status_value=authoritative_terminal_status,
                            ):
                                terminal_message = (
                                    _build_terminal_stream_status_payload(
                                        authoritative_terminal_status,
                                        message=authoritative_terminal_message,
                                    )
                                )
                                yield f"data: {json.dumps(terminal_message)}\n\n"
                        else:
                            error_message = {"type": "status", "status": "error"}
                            yield f"data: {json.dumps(error_message)}\n\n"
                        break

                except asyncio.CancelledError:
                    logger.info(
                        f"Stream generator main loop cancelled for {agent_run_id}"
                    )
                    terminate_stream = True
                    break
                except Exception as loop_err:
                    logger.error(
                        f"Error in stream generator main loop for {agent_run_id}: {loop_err}",
                        exc_info=True,
                    )
                    terminate_stream = True
                    authoritative_terminal_status, authoritative_terminal_message = (
                        await _resolve_latest_authoritative_terminal_truth()
                    )
                    drained_payloads = await _drain_pending_payloads(
                        reason="main_loop_error",
                        authoritative_terminal_status=authoritative_terminal_status,
                    )
                    for payload in drained_payloads:
                        yield payload
                    if authoritative_terminal_status:
                        if not _payloads_include_terminal_status(
                            drained_payloads,
                            status_value=authoritative_terminal_status,
                        ):
                            terminal_message = _build_terminal_stream_status_payload(
                                authoritative_terminal_status,
                                message=authoritative_terminal_message,
                            )
                            yield f"data: {json.dumps(terminal_message)}\n\n"
                    else:
                        error_message = {
                            "type": "status",
                            "status": "error",
                            "message": f"Stream failed: {loop_err}",
                        }
                        yield f"data: {json.dumps(error_message)}\n\n"
                    break

        except Exception as e:
            logger.error(
                f"Error setting up stream for agent run {agent_run_id}: {e}",
                exc_info=True,
            )
            # 如果初始化yield没有发生，则只发送错误状态
            if not initial_yield_complete:
                error_message = {
                    "type": "status",
                    "status": "error",
                    "message": f"Failed to start stream: {e}",
                }
                yield f"data: {json.dumps(error_message)}\n\n"
        finally:
            # 开始逐步的清理释放资源
            terminate_stream = True
            # Graceful shutdown order: unsubscribe → close → cancel
            if pubsub_response:
                try:
                    await pubsub_response.unsubscribe(response_channel)
                except Exception as unsubscribe_error:
                    logger.debug(
                        f"Failed to unsubscribe response channel: {unsubscribe_error}"
                    )
            if pubsub_control:
                try:
                    await pubsub_control.unsubscribe(control_channel)
                except Exception as unsubscribe_error:
                    logger.debug(
                        f"Failed to unsubscribe control channel: {unsubscribe_error}"
                    )
            if pubsub_response:
                try:
                    await pubsub_response.close()
                except Exception as close_error:
                    logger.debug(f"Failed to close response pubsub: {close_error}")
            if pubsub_control:
                try:
                    await pubsub_control.close()
                except Exception as close_error:
                    logger.debug(f"Failed to close control pubsub: {close_error}")

            if listener_task:
                listener_task.cancel()
                try:
                    await listener_task
                except asyncio.CancelledError:
                    logger.info(f"listener_task cancelled")
                except Exception as e:
                    logger.debug(f"listener_task ended with: {e}")
            # 等待片刻，让任务取消
            await asyncio.sleep(0.1)
            logger.debug(f"Streaming cleanup complete for agent run: {agent_run_id}")

    return StreamingResponse(
        stream_generator(agent_run_data),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Content-Type": "text/event-stream",
            "Access-Control-Allow-Origin": "*",
        },
    )


async def generate_and_update_project_name(project_id: str, prompt: str):
    """Generates a project name using an LLM and updates the database."""
    logger.info(f"Starting background task to generate name for project: {project_id}")
    # TODO
    pass


@router.post("/agent/initiate", response_model=InitiateAgentResponse)
async def initiate_agent_with_files(
    prompt: str = Form(...),
    model_name: Optional[str] = Form(None),
    enable_thinking: Optional[bool] = Form(False),
    reasoning_effort: Optional[str] = Form("low"),
    stream: Optional[bool] = Form(True),
    enable_context_manager: Optional[bool] = Form(False),
    shadow_clone_mode: Optional[str] = Form(None),
    shadow_clone_main_model: Optional[str] = Form(None),
    shadow_clone_subagent_model: Optional[str] = Form(None),
    eval_task_id: Optional[str] = Form(None),
    eval_attempt_index: Optional[int] = Form(None),
    eval_max_generated_responses: Optional[int] = Form(None),
    expected_langfuse_trace_id: Optional[str] = Form(None),
    agent_run_id: Optional[str] = Form(None),
    agent_id: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
    prepared_attachments_json: Optional[str] = Form(None),
    files: List[UploadFile] = File(default=[]),
    is_agent_builder: Optional[bool] = Form(False),
    target_agent_id: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """
    Start a new agent session with optional files.

    参数说明:
    - prompt: 用户输入的提示词
    - model_name: The name of the model to use (if None, the default model from configuration will be used)
    - enable_thinking: Whether to enable thinking mode
    - reasoning_effort: The reasoning effort (low/medium/high)
    - stream: Whether to enable streaming response
    - enable_context_manager: Whether to enable context manager
    - agent_id: The specified Agent ID (optional)
    - files: The list of uploaded files
    - is_agent_builder: Whether to use agent builder mode
    - target_agent_id: The target Agent ID (used in builder mode)
    - user_id: The current user ID (from JWT)
    """

    # 打印文件详细信息
    for i, file in enumerate(files):
        logger.info(
            f"Upload Files {i+1}: {file.filename} (size: {file.size if hasattr(file, 'size') else 'unknown'} bytes, type: {file.content_type})"
        )

    global instance_id

    if not instance_id:
        logger.error("Agent API not initialized with instance ID")
        raise HTTPException(
            status_code=500, detail="Agent API not initialized with instance ID"
        )

    # 使用统一的模型解析函数
    try:
        model_config = resolve_model_config(model_name)
        model_name = model_config.model_name
    except ValueError as e:
        logger.error(f"Model resolution failed: {e}")
        raise HTTPException(status_code=400, detail=str(e))

    # 初始化数据库连接
    client = await db.client
    logger.info(f"Database connection successful, account_id: {user_id}")
    prepared_attachments = _parse_prepared_attachments_json(prepared_attachments_json)

    async def load_default_agent():
        """Load or create a default agent for the user."""
        fufanmanus_agent_result = (
            await client.table("agents")
            .select("*")
            .eq("user_id", user_id)
            .eq("metadata->>'is_fufanmanus_default'", "true")
            .execute()
        )

        if fufanmanus_agent_result.data:
            logger.info(
                f"Found FuFanManus default agent: {len(fufanmanus_agent_result.data)} agents"
            )
            default_agent_result = fufanmanus_agent_result
        else:
            default_agent_result = (
                await client.schema("public")
                .table("agents")
                .select("*")
                .eq("user_id", user_id)
                .eq("is_default", True)
                .execute()
            )

        if default_agent_result.data:
            agent_data_local = default_agent_result.data[0]
            version_data_local = None
            if agent_data_local.get("current_version_id"):
                try:
                    logger.info(
                        f"Get default agent version data: {agent_data_local['current_version_id']}"
                    )
                    version_service = await _get_version_service()
                    version_obj = await version_service.get_version(
                        agent_id=agent_data_local["agent_id"],
                        version_id=agent_data_local["current_version_id"],
                        user_id=user_id,
                    )
                    version_data_local = version_obj.to_dict()
                    logger.info(
                        f"Get default agent version data: {version_data_local.get('version_name')}"
                    )
                except Exception as e:
                    logger.warning(f"Get default agent version data failed: {e}")

            logger.info(
                f"Prepare to call extract_agent_config for default agent, whether there is version data: {version_data_local is not None}"
            )
            return extract_agent_config(agent_data_local, version_data_local)

        # 没有默认Agent，尝试创建
        logger.warning(
            f"User {user_id} not found default agent; creating FuFanManus default agent"
        )
        try:
            from agent.fufanmanus.repository import FufanmanusAgentRepository

            repository = FufanmanusAgentRepository()
            created_agent_id = await repository.create_fufanmanus_agent(user_id)

            if created_agent_id:
                default_agent_result = (
                    await client.schema("public")
                    .table("agents")
                    .select("*")
                    .eq("user_id", user_id)
                    .eq("is_default", True)
                    .execute()
                )
                if default_agent_result.data:
                    agent_data_local = default_agent_result.data[0]
                    version_data_local = None
                    logger.info(
                        f"Using created FuFanManus default agent: {agent_data_local.get('name', 'Unknown')} ({agent_data_local.get('agent_id')})"
                    )
                    return extract_agent_config(agent_data_local, version_data_local)
                logger.error(f"Failed to query created FuFanManus default agent")
            else:
                logger.error(f"FuFanManus repository returned no agent_id")
        except Exception as e:
            logger.error(f"Failed to create FuFanManus default agent: {e}")
        return None

    # 4: TODO：加载Agent配置（支持版本管理，注：此版本未实现）
    agent_config = None
    if agent_id:
        logger.info(f"[AGENT INITIATE] Querying for specific agent: {agent_id}")
        agent_result = (
            await client.table("agents")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("user_id", user_id)
            .execute()
        )
        logger.info(
            f"[AGENT INITIATE] Query result: found {len(agent_result.data) if agent_result.data else 0} agents"
        )

        if agent_result.data:
            agent_data = agent_result.data[0]
            logger.info(
                f"[AGENT INITIATE] Agent record summary: {_summary_json(_summarize_agent_record(agent_data))}"
            )

            version_data = None
            if agent_data.get("current_version_id"):
                try:
                    version_service = await _get_version_service()
                    version_obj = await version_service.get_version(
                        agent_id=agent_id,
                        version_id=agent_data["current_version_id"],
                        user_id=user_id,
                    )
                    version_data = version_obj.to_dict()
                    logger.info(
                        f"[AGENT INITIATE] Got version data from version manager: {version_data.get('version_name')}"
                    )
                    logger.info(
                        f"[AGENT INITIATE] Version record summary: {_summary_json(_summarize_version_record(version_data))}"
                    )
                except Exception as e:
                    logger.warning(f"[AGENT INITIATE] Failed to get version data: {e}")

            logger.info(
                f"[AGENT INITIATE] About to call extract_agent_config with version data: {version_data is not None}"
            )

            agent_config = extract_agent_config(agent_data, version_data)
            logger.info(
                f"Agent config summary: {_summary_json(_summarize_agent_config(agent_config))}"
            )
            if version_data:
                logger.info(
                    f"Using custom agent: {agent_config['name']} ({agent_id}) version {agent_config.get('version_name', 'v1')}"
                )
            else:
                logger.info(
                    f"Using custom agent: {agent_config['name']} ({agent_id}) - no version data"
                )
        else:
            logger.warning(
                f"[AGENT INITIATE] Agent {agent_id} not found for user {user_id}; falling back to default"
            )
            agent_config = await load_default_agent()
    else:
        logger.info(f"No agent_id provided, querying default agent")
        agent_config = await load_default_agent()

    if not agent_config:
        raise HTTPException(status_code=404, detail="Agent not found or access denied")

    shadow_clone_model_selection = await _resolve_shadow_clone_model_selection(
        client=client,
        user_id=user_id,
        requested_main_model=shadow_clone_main_model,
        requested_subagent_model=shadow_clone_subagent_model,
    )

    effective_shadow_clone_mode = _resolve_interactive_shadow_clone_mode_value(
        shadow_clone_mode,
    )
    effective_shadow_clone_runtime = _resolve_shadow_clone_runtime_value(
        effective_shadow_clone_mode,
        agent_config=agent_config,
    )
    regular_execution_mode = _resolve_regular_execution_mode(
        effective_shadow_clone_mode,
        shadow_clone_runtime=effective_shadow_clone_runtime,
    )
    queue_regular_run = _should_queue_regular_run(
        effective_shadow_clone_mode,
        shadow_clone_runtime=effective_shadow_clone_runtime,
    )
    requested_run_capacity_cost = get_run_capacity_cost(effective_shadow_clone_mode)
    if is_server_concurrency_budget_enabled() and not queue_regular_run:
        capacity_snapshot = await get_safe_run_capacity_snapshot(
            mode=effective_shadow_clone_mode,
            requested_cost=requested_run_capacity_cost,
        )
        if not bool(capacity_snapshot.get("can_admit")):
            raise HTTPException(
                status_code=429,
                detail=build_run_capacity_limit_detail(
                    mode=effective_shadow_clone_mode,
                    snapshot=capacity_snapshot,
                ),
            )

    request_context = structlog.contextvars.get_contextvars()
    request_id = request_context.get("request_id")
    client_operation_id = request_context.get("client_operation_id")
    agent_run_id = str(uuid.uuid4())
    run_capacity_reservation_owner = ""
    run_capacity_reservation_acquired = False
    agent_run_status_updated = False
    phase2_initial_attempt_created = False

    if regular_execution_mode == "phase2_supervisor":
        pass
    elif queue_regular_run:
        await _ensure_regular_queue_capacity_or_raise(client)
    elif is_server_concurrency_budget_enabled():
        run_capacity_reservation_owner = _build_run_capacity_reservation_owner_token(
            request_id=request_id,
            client_operation_id=client_operation_id,
        )
        await _reserve_run_capacity_or_raise(
            agent_run_id=agent_run_id,
            mode=effective_shadow_clone_mode,
            owner_token=run_capacity_reservation_owner,
        )
        run_capacity_reservation_acquired = True

    # TODO：这里可以添加模型检查，比如模型是否支持访问，用户是否有模型使用权限等，在业务层前做检查
    # 如下是一系列的检查操作：比如
    # 模型连通性：model connectivity check
    # 模型使用权限：model access permission check
    # 模型使用限制：model usage limit check
    # 模型使用计费：model usage billing check
    # 模型使用日志：model usage logging check
    # 模型使用监控：model usage monitoring check
    # 模型使用分析：model usage analysis check

    try:
        # 创建项目并生成项目ID,并插入到数据库中。注意：此操作仅用于初始化占位符
        placeholder_name = (
            f"{prompt[:30]}..."
            if len(prompt) > 30
            else prompt if prompt else "new conversation"
        )
        created_project = False

        if project_id:
            await _load_owned_project(client, project_id=project_id, user_id=user_id)
        else:
            project_id = await _create_placeholder_project(
                client,
                user_id=user_id,
                name=placeholder_name,
            )
            created_project = True

        # 定义变量
        sandbox_id = None
        sandbox = None
        sandbox_pass = None
        vnc_url = None
        website_url = None
        token = None

        # 处理文件此版本未实现，可基于下述代码逻辑自行扩展
        if files and not prepared_attachments:
            # 创建沙盒（懒加载）：只有在文件上传时才立即创建
            logger.info(f"Found {len(files)} files, starting to create sandbox")
            try:
                logger.info("Starting to create sandbox...")
                sandbox_pass = str(uuid.uuid4())
                logger.info("Generated sandbox access secret for project setup")

                # 根据文件类型和用户需求智能选择模板
                sandbox_type = determine_sandbox_type(files)
                logger.info(f"Determined sandbox type: {sandbox_type}")
                sandbox = await create_sandbox(sandbox_pass, project_id, sandbox_type)

                # 获取沙箱ID
                sandbox_info = sandbox.get_info()
                sandbox_id = (
                    sandbox_info.sandbox_id
                    if hasattr(sandbox_info, "sandbox_id")
                    else getattr(sandbox, "id", "unknown")
                )
                logger.info(
                    f"Created sandbox successfully: {sandbox_id} (project: {project_id}, type: {sandbox_type})"
                )

                # 获取访问链接
                logger.info("Getting sandbox access links...")

                # 判断沙箱类型并获取对应的访问链接
                sandbox_name = getattr(sandbox_info, "name", "")
                logger.info(f"Detected sandbox name: {sandbox_name}")

                vnc_url = ""
                website_url = ""
                browser_debug_url = ""

                if sandbox_name == "desktop":
                    #  Desktop 模板 - 使用 stream API 获取 VNC URL
                    try:
                        logger.info("Using desktop stream API to get VNC URL...")
                        url = sandbox.stream.get_url()
                        vnc_url = url
                        logger.info("Desktop VNC URL acquired successfully")
                        # 尝试获取只读模式URL
                        try:
                            readonly_url = sandbox.stream.get_url(view_only=True)
                            logger.info(
                                "Desktop readonly VNC URL acquired successfully"
                            )
                        except Exception as readonly_error:
                            logger.debug(
                                f"Failed to get readonly URL: {readonly_error}"
                            )

                    except Exception as e:
                        logger.error(f"Failed to get desktop VNC URL: {e}")

                # TODO
                elif sandbox_name == "browser-chromium" or sandbox_type == "browser":
                    #  Browser 模板 - 获取 Chrome 调试协议地址
                    try:
                        browser_host = sandbox.get_host(9223)
                        browser_debug_url = f"https://{browser_host}"
                        logger.info("Browser CDP URL acquired successfully")
                    except Exception as e:
                        logger.error(f"Failed to get browser CDP URL: {e}")

                # 更新项目信息
                logger.info("Updating project sandbox information...")
                update_result = (
                    await client.table("projects")
                    .eq("project_id", project_id)
                    .update(
                        {
                            "sandbox": json.dumps(
                                {
                                    "id": sandbox_id,
                                    "pass": sandbox_pass,
                                    "vnc_preview": vnc_url,
                                    "sandbox_url": website_url,
                                    "token": token,
                                }
                            )
                        }
                    )
                )

                if not update_result.data:
                    logger.error(
                        f"Failed to update project {project_id} sandbox information"
                    )
                    if sandbox_id:
                        try:
                            # TODO
                            await delete_sandbox(sandbox_id)
                            logger.info(f"Deleted sandbox {sandbox_id}")
                        except Exception as e:
                            logger.error(f"Failed to delete sandbox: {str(e)}")
                    raise Exception("Database update failed")

                logger.info("Project sandbox information updated successfully")

            except Exception as e:
                logger.error(f"Failed to create sandbox: {str(e)}")
                if created_project:
                    logger.info("Cleaning up created project...")
                    await client.table("projects").eq("project_id", project_id).delete()
                if sandbox_id:
                    try:
                        # TODO
                        await delete_sandbox(sandbox_id)
                        logger.info(f"Deleted sandbox {sandbox_id}")
                    except Exception:
                        pass
                raise Exception("Failed to create sandbox")
        else:
            logger.info("No files uploaded, skipping sandbox creation")

        # 6. 创建线程（thread_id）并做关联
        thread_id = str(uuid.uuid4())

        # 构建关联关系：user_id -> project_id -> thread_id
        thread_data = {
            "thread_id": thread_id,
            "project_id": project_id,
            "account_id": user_id,
            "created_at": datetime.now(),
        }

        # 绑定上下文变量，用于在日志中追踪相关信息
        structlog.contextvars.bind_contextvars(
            thread_id=thread_data["thread_id"],
            project_id=project_id,
            account_id=user_id,
        )

        # 线程是Agent无关的，不存储agent_id
        # Agent选择将在每个消息/Agent运行时处理
        if agent_config:
            logger.info(
                f"Using Agent {agent_config['agent_id']} for conversation (thread remains Agent-agnostic)"
            )
            structlog.contextvars.bind_contextvars(
                agent_id=agent_config["agent_id"],
            )

        # # 如果是Agent构建器会话，存储构建器元数据
        if is_agent_builder:
            print(f"store agent builder metadata: target_agent_id={target_agent_id}")
            thread_data["metadata"] = {
                "is_agent_builder": True,
                "target_agent_id": target_agent_id,
            }
            structlog.contextvars.bind_contextvars(
                target_agent_id=target_agent_id,
            )

        # 插入线程到数据库
        thread = await client.schema("public").table("threads").insert(thread_data)

        if not thread.data:
            logger.error(f"Failed to create thread")
            raise Exception("Failed to create thread")

        # 在创建新的Agent会话时异步触发，通过大模型生成更贴合主题的会话名称
        # TODO：可选。这里可以添加一个任务，通过大模型生成更贴合主题的会话名称，并更新到项目中
        asyncio.create_task(
            generate_and_update_project_name(project_id=project_id, prompt=prompt)
        )

        message_content = prompt
        uploaded_image_refs: List[Dict[str, str]] = []
        if prepared_attachments:
            validated_attachments: List[PreparedAttachmentPayload] = []
            for attachment in prepared_attachments:
                artifact_record = await workspace_artifacts.get_artifact_record(
                    project_id=project_id,
                    path=attachment.path,
                    client=client,
                )
                if artifact_record is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Prepared attachment not found: {attachment.path}",
                    )
                validated_attachments.append(
                    PreparedAttachmentPayload(
                        attachment_id=artifact_record.artifact_id,
                        name=attachment.name or os.path.basename(artifact_record.path),
                        original_name=attachment.original_name,
                        path=artifact_record.path,
                        size=artifact_record.size_bytes,
                        content_type=attachment.content_type
                        or artifact_record.content_type,
                        kind=attachment.kind,
                        mime_type=attachment.mime_type,
                        filename=attachment.filename
                        or attachment.name
                        or os.path.basename(artifact_record.path),
                        sha256=artifact_record.sha256,
                    )
                )

            if validated_attachments:
                message_content += "\n\n" if message_content else ""
                for attachment in validated_attachments:
                    message_content += f"[用户上传文件: {attachment.path}]\n"
                    image_ref = _prepared_attachment_to_image_ref(attachment)
                    if image_ref is not None:
                        uploaded_image_refs.append(image_ref)
                logger.info(
                    f"Prepared attachments reused successfully: {len(validated_attachments)} files"
                )
        # 处理上传文件到沙盒环境（此版本未实现，可基于下述代码逻辑自行扩展）
        elif files:
            successful_uploads: List[Dict[str, Any]] = []
            failed_uploads = []

            for i, file in enumerate(files):
                logger.info(f"Processing file {i+1}/{len(files)}: {file.filename}")

                if file.filename:
                    try:
                        safe_filename = file.filename.replace("/", "_").replace(
                            "\\", "_"
                        )
                        target_path = f"/workspace/{safe_filename}"

                        content = await file.read()
                        logger.info(f"files read success, size: {len(content)} bytes")

                        image_ref: Optional[Dict[str, str]] = None
                        if is_image_upload(
                            file.content_type or "", file.filename or ""
                        ):
                            mime_type = (file.content_type or "").strip().lower()
                            if not mime_type.startswith("image/"):
                                mime_type = "image/png"
                            image_ref = {
                                "kind": "image",
                                "path": target_path,
                                "mime_type": mime_type,
                                "filename": safe_filename,
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }

                        upload_successful = False
                        try:
                            # 使用 PPIO 推荐的方法: sandbox.files.write()
                            if hasattr(sandbox, "files") and hasattr(
                                sandbox.files, "write"
                            ):
                                logger.info(f"Uploading file to sandbox: {target_path}")
                                # 根据 PPIO 官方文档，files.write() 是同步方法，不需要 await
                                sandbox.files.write(target_path, content)
                                logger.info(
                                    f"File uploaded successfully: {target_path}"
                                )
                                upload_successful = True
                            else:
                                logger.error(
                                    f"Sandbox object missing file upload method"
                                )
                                raise NotImplementedError(
                                    "No suitable upload method found on sandbox object."
                                )

                        except Exception as upload_error:
                            logger.error(
                                f"Sandbox upload failed {safe_filename}: {str(upload_error)}"
                            )
                            logger.debug(
                                f"Sandbox upload error details: {upload_error}"
                            )  # 使用 debug 记录详细错误

                        if upload_successful:
                            try:
                                await workspace_artifacts.persist_artifact(
                                    project_id=project_id,
                                    path=target_path,
                                    content=content,
                                    source="agent_api.initiate_upload",
                                    client=client,
                                    created_by_user_id=user_id,
                                    content_type=file.content_type or None,
                                    metadata={
                                        "filename": safe_filename,
                                        "upload_origin": "agent_initiate",
                                    },
                                )
                            except Exception as persist_error:
                                logger.warning(
                                    "Failed to persist uploaded workspace artifact for project=%s path=%s: %s",
                                    project_id,
                                    target_path,
                                    persist_error,
                                )
                            try:
                                logger.info(f"Verifying file upload...")
                                await asyncio.sleep(0.2)

                                # 使用 PPIO 正确的 API 验证文件
                                if hasattr(sandbox, "files") and hasattr(
                                    sandbox.files, "exists"
                                ):
                                    # 检查文件是否存在
                                    file_exists = sandbox.files.exists(target_path)
                                    if file_exists:
                                        successful_uploads.append(
                                            {
                                                "path": target_path,
                                                "image_ref": image_ref,
                                            }
                                        )
                                        logger.info(
                                            f"File uploaded and verified successfully: {safe_filename} -> {target_path}"
                                        )
                                    else:
                                        logger.error(
                                            f"File verification failed: {target_path} does not exist"
                                        )
                                        failed_uploads.append(safe_filename)
                                else:
                                    # 如果没有 exists 方法，直接标记为成功（已经成功上传了）
                                    successful_uploads.append(
                                        {"path": target_path, "image_ref": image_ref}
                                    )
                                    logger.info(
                                        f"File uploaded successfully (skip verification): {safe_filename} -> {target_path}"
                                    )

                            except Exception as verify_error:
                                # 验证失败不影响上传，标记为成功
                                successful_uploads.append(
                                    {"path": target_path, "image_ref": image_ref}
                                )
                                logger.warning(
                                    f"File verification failed but upload was successful {safe_filename}: {str(verify_error)}"
                                )
                                logger.debug(
                                    f"File verification error details: {verify_error}"
                                )  # 使用 debug 避免 exc_info 问题
                        else:
                            failed_uploads.append(safe_filename)
                    except Exception as file_error:
                        logger.error(
                            f"File processing failed {file.filename}: {str(file_error)}"
                        )
                        logger.debug(
                            f"File processing error details: {file_error}"
                        )  # 使用 debug 记录详细错误
                        failed_uploads.append(file.filename)
                    finally:
                        await file.close()
                        logger.info(f"File closed: {file.filename}")

            # 更新消息内容
            if successful_uploads:
                message_content += "\n\n" if message_content else ""
                for upload_info in successful_uploads:
                    file_path = str(upload_info.get("path") or "")
                    if file_path:
                        message_content += f"[用户上传文件: {file_path}]\n"

                    image_ref = upload_info.get("image_ref")
                    if isinstance(image_ref, dict):
                        uploaded_image_refs.append(image_ref)
                logger.info(
                    f"File uploaded successfully: {len(successful_uploads)} files"
                )

            if failed_uploads:
                message_content += "\n\nThe following files failed to upload:\n"
                for failed_file in failed_uploads:
                    message_content += f"- {failed_file}\n"
                logger.warning(f"File upload failed: {len(failed_uploads)} files")

            logger.info(
                f"Prepared user message summary: {_summary_json(_summarize_message_content(message_content))}"
            )
        else:
            logger.info("No files to upload")

        # 添加初始用户消息到线程
        message_payload = {"role": "user", "content": message_content}
        logger.info(
            f"New user message summary: {_summary_json(_summarize_message_content(message_content))}"
        )

        # 在ADK架构中，使用thread_id作为session_id
        adk_session_id = thread_id

        # 创建ADK session（如果不存在）
        await _create_adk_session_if_not_exists(client, user_id, adk_session_id)
        logger.info(f"Created ADK session successfully: {adk_session_id}")

        # # 使用ADK events表记录消息
        message_id = str(uuid.uuid4())
        await _log_adk_user_message_event(
            client,
            user_id,
            message_content,
            adk_session_id,
            message_id,
            media_refs=uploaded_image_refs,
        )
        logger.info(f"User message event recorded successfully: {message_id}")

        # 确定最终使用的模型
        # 模型选择的优先级逻辑
        # model_name ：用户在前端选择的模型
        # agent_config.model ：用户在Agent配置中选择的模型
        # MODEL_NAME_ALIASES，即config.MODEL_TO_USE，模型别名映射，在.ENV 文件中获取

        # 优先级：用户在前端选择的模型 > Agent配置中选择的模型 > 模型别名映射
        # 如果用户在前端选择的模型在MODEL_NAME_ALIASES中存在，则使用MODEL_NAME_ALIASES中的模型
        # 如果用户在前端选择的模型在MODEL_NAME_ALIASES中不存在，则使用用户在前端选择的模型
        # 如果用户在Agent配置中选择的模型在MODEL_NAME_ALIASES中存在，则使用MODEL_NAME_ALIASES中的模型
        effective_model = model_name
        if not model_name and agent_config and agent_config.get("model"):
            effective_model = agent_config["model"]
            logger.info(
                f"User did not specify model, using Agent configured model: {effective_model}"
            )
        elif model_name:
            logger.info(f"Using user selected model: {effective_model}")
        else:
            logger.info(f"Using default model: {effective_model}")

        shadow_clone_requested_mode = effective_shadow_clone_mode
        qwen_guard_model = (
            shadow_clone_model_selection["effective_shadow_clone_main_model"]
            if _is_shadow_clone_model_policy_mode(shadow_clone_requested_mode)
            else None
        ) or effective_model
        if _is_qwen_35_model(qwen_guard_model):
            await _enforce_single_active_qwen_run(client, thread_id)

        # 完成模型别名解析，适配 LiteLLM 的规范
        resolved_model = MODEL_NAME_ALIASES.get(effective_model, effective_model)
        effective_enable_thinking, effective_reasoning_effort = (
            _normalize_model_reasoning_controls(
                resolved_model,
                enable_thinking,
                reasoning_effort,
            )
        )

        agent_run_metadata = {
            "model_name": resolved_model,  # 使用解析后的模型名
            "requested_model": model_name,  # 保留用户原始请求
            "enable_thinking": effective_enable_thinking,
            "reasoning_effort": effective_reasoning_effort,
            "enable_context_manager": enable_context_manager,
            "requested_shadow_clone_mode": shadow_clone_mode,
            "shadow_clone_mode": effective_shadow_clone_mode,
            "shadow_clone_runtime": effective_shadow_clone_runtime,
            "requested_shadow_clone_main_model": shadow_clone_model_selection[
                "requested_shadow_clone_main_model"
            ],
            "requested_shadow_clone_subagent_model": shadow_clone_model_selection[
                "requested_shadow_clone_subagent_model"
            ],
            "shadow_clone_main_model": shadow_clone_model_selection[
                "effective_shadow_clone_main_model"
            ],
            "shadow_clone_subagent_model": shadow_clone_model_selection[
                "effective_shadow_clone_subagent_model"
            ],
        }
        if request_id:
            agent_run_metadata["request_id"] = request_id
        if client_operation_id:
            agent_run_metadata["client_operation_id"] = client_operation_id
        if effective_shadow_clone_runtime == "v2":
            agent_run_metadata["shadow_clone_v2_execution_chain"] = (
                _resolve_shadow_clone_v2_execution_chain()
            )
        normalized_eval_task_id = str(eval_task_id or "").strip()
        if normalized_eval_task_id:
            agent_run_metadata["eval_task_id"] = normalized_eval_task_id
        if eval_attempt_index is not None:
            agent_run_metadata["eval_attempt_index"] = eval_attempt_index
        if eval_max_generated_responses is not None:
            agent_run_metadata["eval_max_generated_responses"] = (
                eval_max_generated_responses
            )
        normalized_expected_trace_id = str(expected_langfuse_trace_id or "").strip()
        if normalized_expected_trace_id:
            agent_run_metadata["expected_langfuse_trace_id"] = (
                normalized_expected_trace_id
            )
        if regular_execution_mode == "phase1_pool":
            agent_run_metadata["regular_async_pool"] = True
        elif regular_execution_mode == "phase2_supervisor":
            agent_run_metadata["regular_execution_mode"] = regular_execution_mode

        if queue_regular_run and regular_execution_mode == "phase2_supervisor":
            queued_run = await regular_run_admission.admit_queued_regular_run(
                client=client,
                thread_id=thread_id,
                project_id=project_id,
                agent_run_id=agent_run_id,
                agent_config_snapshot=agent_config,
                agent_run_metadata=agent_run_metadata,
                execution_mode=regular_execution_mode,
                ensure_phase2_attempt_queue_capacity=_ensure_phase2_attempt_queue_capacity_or_raise,
                create_initial_attempt=regular_run_attempts.create_initial_attempt,
                dispatch_queue_handoff=_dispatch_regular_queue_handoff,
                mark_phase2_admission_attempt_failed=_mark_phase2_admission_attempt_failed_best_effort,
                update_agent_run_status=update_agent_run_status,
            )
            return {
                "thread_id": thread_id,
                "agent_run_id": queued_run["agent_run_id"],
            }

        initial_status = "queued" if queue_regular_run else "running"
        # 存储Agent运行记录到数据库中
        agent_run = (
            await client.schema("public")
            .table("agent_runs")
            .insert(
                {
                    "agent_run_id": agent_run_id,
                    "thread_id": thread_id,
                    "status": initial_status,
                    "started_at": datetime.now(),
                    "agent_id": agent_config.get("agent_id") if agent_config else None,
                    "agent_version_id": (
                        agent_config.get("current_version_id") if agent_config else None
                    ),
                    "metadata": json.dumps(agent_run_metadata),
                }
            )
        )

        if not agent_run.data:
            logger.error(f"Failed to create agent run")
            raise Exception("Failed to create agent run")

        agent_run_id = str(
            agent_run.data[0].get("agent_run_id") or agent_run.data[0]["id"]
        )

        # 绑定Agent运行ID到上下文，用于在日志中追踪相关信息
        structlog.contextvars.bind_contextvars(
            agent_run_id=agent_run_id,
        )

        if queue_regular_run:
            _dispatch_regular_queue_handoff(execution_mode=regular_execution_mode)
            return {"thread_id": thread_id, "agent_run_id": agent_run_id}

        # 9. 在Redis中注册运行
        instance_key = f"active_run:{instance_id}:{agent_run_id}"
        try:
            await redis.set(instance_key, "running", ex=redis.REDIS_KEY_TTL)
            logger.info(f"Redis registered successfully: {instance_key}")
        except Exception as e:
            logger.error(f"Redis registered failed ({instance_key}): {str(e)}")

        # 获取请求ID并启动后台Agent
        # 11. 发送Agent运行任务到后台，这里才是真正开始执行Agent的逻辑
        # 注意：这里不需要传递用户的请求，因为需要在后续的处理中通过查询数据库来获取
        try:
            # 让 Agent 运行任务进入 Dramatiq 任务队列，等待被 worker 执行
            message = run_agent_background.send(
                agent_run_id=agent_run_id,
                thread_id=thread_id,
                instance_id=instance_id,
                project_id=project_id,
                model_name=resolved_model,
                enable_thinking=effective_enable_thinking,
                reasoning_effort=effective_reasoning_effort,
                stream=stream,
                enable_context_manager=enable_context_manager,
                agent_config=agent_config,
                is_agent_builder=is_agent_builder,
                target_agent_id=target_agent_id,
                shadow_clone_mode=effective_shadow_clone_mode,
                shadow_clone_main_model=shadow_clone_model_selection[
                    "effective_shadow_clone_main_model"
                ],
                shadow_clone_subagent_model=shadow_clone_model_selection[
                    "effective_shadow_clone_subagent_model"
                ],
                request_id=request_id,
            )
            logger.info(
                f"Agent run task sent to background: message_id={message.message_id} "
                f"request_id={request_id or '-'} client_operation_id={client_operation_id or '-'}"
            )
        except Exception as send_error:
            if run_capacity_reservation_acquired:
                await _release_run_capacity_reservation_best_effort(
                    agent_run_id=agent_run_id,
                    owner_token=run_capacity_reservation_owner,
                )
                run_capacity_reservation_acquired = False
            await update_agent_run_status(
                client,
                agent_run_id,
                "failed",
                error=f"Failed to dispatch agent run: {send_error}",
            )
            agent_run_status_updated = True
            logger.error(f"Failed to send background task: {send_error}")
            raise

        return {"thread_id": thread_id, "agent_run_id": agent_run_id}

    except HTTPException as http_error:
        if (
            "run_capacity_reservation_acquired" in locals()
            and run_capacity_reservation_acquired
        ):
            await _release_run_capacity_reservation_best_effort(
                agent_run_id=agent_run_id,
                owner_token=run_capacity_reservation_owner,
            )
            run_capacity_reservation_acquired = False
        raise http_error
    except Exception as e:
        if (
            "run_capacity_reservation_acquired" in locals()
            and run_capacity_reservation_acquired
        ):
            await _release_run_capacity_reservation_best_effort(
                agent_run_id=agent_run_id,
                owner_token=run_capacity_reservation_owner,
            )
            run_capacity_reservation_acquired = False
        if phase2_initial_attempt_created:
            await _mark_phase2_admission_attempt_failed_best_effort(
                client,
                agent_run_id=agent_run_id,
                error_message=f"Failed to dispatch agent run: {e}",
            )
        if (
            "agent_run" in locals()
            and getattr(agent_run, "data", None)
            and "agent_run_id" in locals()
            and agent_run_id
            and not agent_run_status_updated
        ):
            await update_agent_run_status(
                client,
                agent_run_id,
                "failed",
                error=f"Failed to dispatch agent run: {e}",
            )
            agent_run_status_updated = True
        logger.error(f"Error in agent initiation: {str(e)}\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500, detail=f"Failed to initiate agent session: {str(e)}"
        )


# Custom agents
@router.get("/agents", response_model=AgentsResponse)
async def get_agents(
    user_id: str = Depends(get_current_user_id_from_jwt),
    page: Optional[int] = Query(1, ge=1, description="Page number (1-based)"),
    limit: Optional[int] = Query(
        20, ge=1, le=100, description="Number of items per page"
    ),
    search: Optional[str] = Query(None, description="Search in name and description"),
    sort_by: Optional[str] = Query(
        "created_at",
        description="Sort field: name, created_at, updated_at, tools_count",
    ),
    sort_order: Optional[str] = Query("desc", description="Sort order: asc, desc"),
    has_default: Optional[bool] = Query(None, description="Filter by default agents"),
    has_mcp_tools: Optional[bool] = Query(
        None, description="Filter by agents with MCP tools"
    ),
    has_agentpress_tools: Optional[bool] = Query(
        None, description="Filter by agents with AgentPress tools"
    ),
    tools: Optional[str] = Query(
        None, description="Comma-separated list of tools to filter by"
    ),
):
    """Get agents for the current user with pagination, search, sort, and filter support."""
    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agents currently disabled. This feature is not available at the moment."
    #     )
    logger.info(
        f"Fetching agents for user: {user_id} with page={page}, limit={limit}, search='{search}', sort_by={sort_by}, sort_order={sort_order}"
    )
    client = await db.client

    try:
        # 计算偏移量
        offset = (page - 1) * limit
        # 构建基础查询：选择 agents 表的所有字段 (*)
        # 启用精确计数 (count='exact') 用于分页
        # 过滤条件：只查询当前用户的 agents
        query = client.table("agents").select("*", count="exact").eq("user_id", user_id)

        # 如果提供搜索词，在 name 和 description 字段中模糊搜索
        if search:
            search_term = f"%{search}%"  # 模糊匹配模式
            query = query.or_(
                f"name.ilike.{search_term},description.ilike.{search_term}"
            )

        # 过滤条件：是否为默认 Agent，只有明确传入 True/False 时才应用此过滤
        if has_default is not None:
            query = query.eq("is_default", has_default)

        # 支持按 name、updated_at、created_at 排序
        # 支持升序(asc)和降序(desc)
        # 默认按创建时间降序排列（最新的在前）
        if sort_by == "name":
            query = query.order("name", desc=(sort_order == "desc"))
        elif sort_by == "updated_at":
            query = query.order("updated_at", desc=(sort_order == "desc"))
        elif sort_by == "created_at":
            query = query.order("created_at", desc=(sort_order == "desc"))
        else:
            # 默认按创建时间排序
            query = query.order("created_at", desc=(sort_order == "desc"))

        # 获取分页数据和总数量
        query = query.range(offset, offset + limit - 1)
        agents_result = await query.execute()
        total_count = agents_result.count if agents_result.count is not None else 0

        if not agents_result.data:
            logger.info(f"No agents found for user: {user_id}")
            return {
                "agents": [],
                "pagination": {"page": page, "limit": limit, "total": 0, "pages": 0},
            }

        # 后处理：工具过滤和tools_count排序
        agents_data = agents_result.data

        # 首先，批量获取所有Agent的版本数据，确保我们拥有正确的工具信息
        # 这样做比逐个Agent调用服务更高效
        agent_version_map = {}
        version_ids = list(
            {
                agent["current_version_id"]
                for agent in agents_data
                if agent.get("current_version_id")
            }
        )
        logger.info(f"version_ids: {version_ids}")
        if version_ids:
            try:
                versions_result = (
                    await client.table("agent_versions")
                    .select(
                        "version_id, agent_id, version_number, version_name, is_active, created_at, updated_at, created_by, config"
                    )
                    .in_("version_id", version_ids)
                    .execute()
                )

                for row in versions_result.data or []:
                    config = row.get("config") or {}
                    tools = config.get("tools") or {}
                version_dict = {
                    "version_id": row["version_id"],
                    "agent_id": row["agent_id"],
                    "version_number": row["version_number"],
                    "version_name": row["version_name"],
                    "system_prompt": config.get("system_prompt", ""),
                    "configured_mcps": tools.get("mcp", []),
                    "custom_mcps": tools.get("custom_mcp", []),
                    "agentpress_tools": tools.get("agentpress", {}),
                    "is_active": row.get("is_active", False),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at") or row.get("created_at"),
                    "created_by": row.get("created_by"),
                }
                agent_version_map[row["agent_id"]] = version_dict
            except Exception as e:
                logger.warning(f"Failed to batch load versions for agents: {e}")

        # 应用工具过滤条件使用版本数据
        if has_mcp_tools is not None or has_agentpress_tools is not None or tools:
            filtered_agents = []
            tools_filter = []
            if tools:
                # 处理tools参数可能作为dict而不是字符串传递的情况
                if isinstance(tools, str):
                    tools_filter = [
                        tool.strip() for tool in tools.split(",") if tool.strip()
                    ]
                elif isinstance(tools, dict):
                    # 如果tools是dict，记录问题并跳过过滤
                    logger.warning(
                        f"Received tools parameter as dict instead of string: {tools}"
                    )
                    tools_filter = []
                elif isinstance(tools, list):
                    # 如果tools是list，直接使用
                    tools_filter = [
                        str(tool).strip() for tool in tools if str(tool).strip()
                    ]
                else:
                    logger.warning(
                        f"Unexpected tools parameter type: {type(tools)}, value: {tools}"
                    )
                    tools_filter = []

            for agent in agents_data:
                # Get version data if available and extract configuration
                version_data = agent_version_map.get(agent["agent_id"])
                from agent.config_helper import extract_agent_config

                agent_config = extract_agent_config(agent, version_data)

                configured_mcps = agent_config["configured_mcps"]
                agentpress_tools = agent_config["agentpress_tools"]

                # Check MCP tools filter
                if has_mcp_tools is not None:
                    has_mcp = bool(configured_mcps and len(configured_mcps) > 0)
                    if has_mcp_tools != has_mcp:
                        continue

                # Check AgentPress tools filter
                if has_agentpress_tools is not None:
                    has_enabled_tools = any(
                        tool_data
                        and isinstance(tool_data, dict)
                        and tool_data.get("enabled", False)
                        for tool_data in agentpress_tools.values()
                    )
                    if has_agentpress_tools != has_enabled_tools:
                        continue

                # Check specific tools filter
                if tools_filter:
                    agent_tools = set()
                    # Add MCP tools
                    for mcp in configured_mcps:
                        if isinstance(mcp, dict) and "name" in mcp:
                            agent_tools.add(f"mcp:{mcp['name']}")

                    # Add enabled AgentPress tools
                    for tool_name, tool_data in agentpress_tools.items():
                        if (
                            tool_data
                            and isinstance(tool_data, dict)
                            and tool_data.get("enabled", False)
                        ):
                            agent_tools.add(f"agentpress:{tool_name}")

                    # Check if any of the requested tools are present
                    if not any(tool in agent_tools for tool in tools_filter):
                        continue

                filtered_agents.append(agent)

            agents_data = filtered_agents

        # 处理tools_count排序 (后处理 required)
        if sort_by == "tools_count":

            def get_tools_count(agent):
                # 获取版本数据如果available
                version_data = agent_version_map.get(agent["agent_id"])

                # 使用版本数据用于工具如果available, 否则回退到Agent数据
                if version_data:
                    configured_mcps = version_data.get("configured_mcps", [])
                    agentpress_tools = version_data.get("agentpress_tools", {})
                else:
                    configured_mcps = agent.get("configured_mcps", [])
                    agentpress_tools = agent.get("agentpress_tools", {})

                mcp_count = len(configured_mcps)
                agentpress_count = sum(
                    1
                    for tool_data in agentpress_tools.values()
                    if tool_data
                    and isinstance(tool_data, dict)
                    and tool_data.get("enabled", False)
                )
                return mcp_count + agentpress_count

            agents_data.sort(key=get_tools_count, reverse=(sort_order == "desc"))

        # 应用分页到过滤结果如果我们做了后处理
        if (
            has_mcp_tools is not None
            or has_agentpress_tools is not None
            or tools
            or sort_by == "tools_count"
        ):
            total_count = len(agents_data)
            agents_data = agents_data[offset : offset + limit]

        # 格式化响应
        agent_list = []
        for agent in agents_data:
            current_version = None
            # 使用已经获取的版本数据 from agent_version_map
            version_dict = agent_version_map.get(agent["agent_id"])
            if version_dict:
                try:
                    current_version = AgentVersionResponse(
                        version_id=version_dict["version_id"],
                        agent_id=version_dict["agent_id"],
                        version_number=version_dict["version_number"],
                        version_name=version_dict["version_name"],
                        system_prompt=version_dict["system_prompt"],
                        model=version_dict.get("model"),
                        configured_mcps=version_dict.get("configured_mcps", []),
                        custom_mcps=version_dict.get("custom_mcps", []),
                        agentpress_tools=version_dict.get("agentpress_tools", {}),
                        is_active=version_dict.get("is_active", True),
                        created_at=version_dict["created_at"],
                        updated_at=version_dict.get(
                            "updated_at", version_dict["created_at"]
                        ),
                        created_by=version_dict.get("created_by"),
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to get version data for agent {agent['agent_id']}: {e}"
                    )

            # 提取配置使用统一配置 approach
            from agent.config_helper import extract_agent_config

            agent_config = extract_agent_config(agent, version_dict)

            system_prompt = agent_config["system_prompt"]
            configured_mcps = agent_config["configured_mcps"]
            custom_mcps = agent_config["custom_mcps"]
            agentpress_tools = agent_config["agentpress_tools"]

            agent_list.append(
                AgentResponse(
                    agent_id=agent["agent_id"],
                    account_id=agent["user_id"],
                    name=agent["name"],
                    description=agent.get("description"),
                    system_prompt=system_prompt,
                    configured_mcps=configured_mcps,
                    custom_mcps=custom_mcps,
                    agentpress_tools=agentpress_tools,
                    is_default=agent.get("is_default", False),
                    is_public=agent.get("is_public", False),
                    tags=agent.get("tags", []),
                    avatar=agent_config.get("avatar"),
                    avatar_color=agent_config.get("avatar_color"),
                    profile_image_url=agent_config.get("profile_image_url"),
                    created_at=(
                        agent["created_at"].isoformat()
                        if agent.get("created_at")
                        else None
                    ),
                    updated_at=(
                        agent["updated_at"].isoformat()
                        if agent.get("updated_at")
                        else None
                    ),
                    current_version_id=agent.get("current_version_id"),
                    version_count=agent.get("version_count", 1),
                    current_version=current_version,
                    metadata=(
                        json.loads(agent.get("metadata", "{}"))
                        if isinstance(agent.get("metadata"), str)
                        else (agent.get("metadata") or {})
                    ),
                )
            )

        total_pages = (total_count + limit - 1) // limit

        logger.info(
            f"Found {len(agent_list)} agents for user: {user_id} (page {page}/{total_pages})"
        )
        return {
            "agents": agent_list,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "pages": total_pages,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching agents for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch agents: {str(e)}")


@router.get("/agents/{agent_id}", response_model=AgentResponse)
async def get_agent(
    agent_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Get a specific agent by ID with current version information. Only the owner can access non-public agents."""
    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agents currently disabled. This feature is not available at the moment."
    #     )

    logger.info(f"Fetching agent {agent_id} for user: {user_id}")
    client = await db.client

    try:
        # Get agent
        agent_result = (
            await client.table("agents").select("*").eq("agent_id", agent_id).execute()
        )

        if not agent_result.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent_data = agent_result.data[0]

        # Check ownership - only owner can access non-public agents
        if agent_data["user_id"] != user_id and not agent_data.get("is_public", False):
            raise HTTPException(status_code=403, detail="Access denied")

        # Use versioning system to get current version data
        current_version = None
        if agent_data.get("current_version_id"):
            try:
                version_service = await _get_version_service()
                current_version_obj = await version_service.get_version(
                    agent_id=agent_id,
                    version_id=agent_data["current_version_id"],
                    user_id=user_id,
                )
                current_version_data = current_version_obj.to_dict()
                version_data = current_version_data

                # Create AgentVersionResponse from version data
                current_version = AgentVersionResponse(
                    version_id=current_version_data["version_id"],
                    agent_id=current_version_data["agent_id"],
                    version_number=current_version_data["version_number"],
                    version_name=current_version_data["version_name"],
                    system_prompt=current_version_data["system_prompt"],
                    model=current_version_data.get("model"),
                    configured_mcps=current_version_data.get("configured_mcps", []),
                    custom_mcps=current_version_data.get("custom_mcps", []),
                    agentpress_tools=current_version_data.get("agentpress_tools", {}),
                    is_active=current_version_data.get("is_active", True),
                    created_at=current_version_data["created_at"],
                    updated_at=current_version_data.get(
                        "updated_at", current_version_data["created_at"]
                    ),
                    created_by=current_version_data.get("created_by"),
                )

                logger.info(
                    f"Using agent {agent_data['name']} version {current_version_data.get('version_name', 'v1')}"
                )
            except Exception as e:
                logger.warning(f"Failed to get version data for agent {agent_id}: {e}")

        # Extract configuration using the unified config approach
        version_data = None
        if current_version:
            version_data = {
                "version_id": current_version.version_id,
                "agent_id": current_version.agent_id,
                "version_number": current_version.version_number,
                "version_name": current_version.version_name,
                "system_prompt": current_version.system_prompt,
                "model": current_version.model,
                "configured_mcps": current_version.configured_mcps,
                "custom_mcps": current_version.custom_mcps,
                "agentpress_tools": current_version.agentpress_tools,
                "is_active": current_version.is_active,
                "created_at": current_version.created_at,
                "updated_at": current_version.updated_at,
                "created_by": current_version.created_by,
            }

        from agent.config_helper import extract_agent_config

        agent_config = extract_agent_config(agent_data, version_data)

        system_prompt = agent_config["system_prompt"]
        configured_mcps = agent_config["configured_mcps"]
        custom_mcps = agent_config["custom_mcps"]
        agentpress_tools = agent_config["agentpress_tools"]

        return AgentResponse(
            agent_id=agent_data["agent_id"],
            account_id=agent_data["user_id"],
            name=agent_data["name"],
            description=agent_data.get("description"),
            system_prompt=system_prompt,
            configured_mcps=configured_mcps,
            custom_mcps=custom_mcps,
            agentpress_tools=agentpress_tools,
            is_default=agent_data.get("is_default", False),
            is_public=agent_data.get("is_public", False),
            tags=agent_data.get("tags", []),
            avatar=agent_config.get("avatar"),
            avatar_color=agent_config.get("avatar_color"),
            profile_image_url=agent_config.get("profile_image_url"),
            created_at=(
                agent_data["created_at"].isoformat()
                if agent_data.get("created_at")
                else None
            ),
            updated_at=(
                agent_data.get("updated_at", agent_data["created_at"]).isoformat()
                if agent_data.get("updated_at", agent_data["created_at"])
                else None
            ),
            current_version_id=agent_data.get("current_version_id"),
            version_count=agent_data.get("version_count", 1),
            current_version=current_version,
            metadata=(
                json.loads(agent_data.get("metadata", "{}"))
                if isinstance(agent_data.get("metadata"), str)
                else (agent_data.get("metadata") or {})
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching agent {agent_id} for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch agent: {str(e)}")


@router.get("/agents/{agent_id}/export")
async def export_agent(
    agent_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Export an agent configuration as JSON"""
    logger.info(f"Exporting agent {agent_id} for user: {user_id}")

    try:
        client = await db.client

        # Get agent data
        agent_result = (
            await client.table("agents")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("account_id", user_id)
            .execute()
        )
        if not agent_result.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent = agent_result.data[0]

        # Get current version data if available
        current_version = None
        if agent.get("current_version_id"):
            version_result = (
                await client.table("agent_versions")
                .select("*")
                .eq("version_id", agent["current_version_id"])
                .execute()
            )
            if version_result.data:
                current_version = version_result.data[0]

        from agent.config_helper import extract_agent_config

        config = extract_agent_config(agent, current_version)

        from templates.template_service import TemplateService

        template_service = TemplateService(db)

        full_config = {
            "system_prompt": config.get("system_prompt", ""),
            "tools": {
                "agentpress": config.get("agentpress_tools", {}),
                "mcp": config.get("configured_mcps", []),
                "custom_mcp": config.get("custom_mcps", []),
            },
            "metadata": {
                # keep backward compat metadata
                "avatar": config.get("avatar"),
                "avatar_color": config.get("avatar_color"),
                # include profile image url in metadata for completeness
                "profile_image_url": agent.get("profile_image_url"),
            },
        }

        sanitized_config = template_service._fallback_sanitize_config(full_config)

        export_metadata = {}
        if agent.get("metadata"):
            export_metadata = {
                k: v
                for k, v in agent["metadata"].items()
                if k
                not in [
                    "is_fufanmanus_default",
                    "centrally_managed",
                    "installation_date",
                    "last_central_update",
                ]
            }

        export_data = {
            "tools": sanitized_config["tools"],
            "metadata": sanitized_config["metadata"],
            "system_prompt": sanitized_config["system_prompt"],
            "name": config.get("name", ""),
            "description": config.get("description", ""),
            # Deprecated
            "avatar": config.get("avatar"),
            "avatar_color": config.get("avatar_color"),
            # New
            "profile_image_url": agent.get("profile_image_url"),
            "tags": agent.get("tags", []),
            "export_metadata": export_metadata,
            "exported_at": datetime.now(timezone.utc).isoformat(),
        }

        logger.info(f"Successfully exported agent {agent_id}")
        return export_data

    except Exception as e:
        logger.error(f"Error exporting agent {agent_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to export agent: {str(e)}")


# JSON Import endpoints - similar to template installation flow


class JsonAnalysisRequest(BaseModel):
    """Request to analyze JSON for import requirements"""

    json_data: Dict[str, Any]


class JsonAnalysisResponse(BaseModel):
    """Response from JSON analysis"""

    requires_setup: bool
    missing_regular_credentials: List[Dict[str, Any]] = []
    missing_custom_configs: List[Dict[str, Any]] = []
    agent_info: Dict[str, Any] = {}


class JsonImportRequestModel(BaseModel):
    """Request to import agent from JSON"""

    json_data: Dict[str, Any]
    instance_name: Optional[str] = None
    custom_system_prompt: Optional[str] = None
    profile_mappings: Optional[Dict[str, str]] = None
    custom_mcp_configs: Optional[Dict[str, Dict[str, Any]]] = None


class JsonImportResponse(BaseModel):
    """Response from JSON import"""

    status: str
    instance_id: Optional[str] = None
    name: Optional[str] = None
    missing_regular_credentials: List[Dict[str, Any]] = []
    missing_custom_configs: List[Dict[str, Any]] = []
    agent_info: Dict[str, Any] = {}


@router.post("/agents/json/analyze", response_model=JsonAnalysisResponse)
async def analyze_json_for_import(
    request: JsonAnalysisRequest, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Analyze imported JSON to determine required credentials and configurations"""
    logger.info(f"Analyzing JSON for import - user: {user_id}")

    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agents currently disabled. This feature is not available at the moment."
    #     )

    try:
        from agent.json_import_service import JsonImportService

        import_service = JsonImportService(db)

        analysis = await import_service.analyze_json(request.json_data, user_id)

        return JsonAnalysisResponse(
            requires_setup=analysis.requires_setup,
            missing_regular_credentials=analysis.missing_regular_credentials,
            missing_custom_configs=analysis.missing_custom_configs,
            agent_info=analysis.agent_info,
        )

    except Exception as e:
        logger.error(f"Error analyzing JSON: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Failed to analyze JSON: {str(e)}")


@router.post("/agents/json/import", response_model=JsonImportResponse)
async def import_agent_from_json(
    request: JsonImportRequestModel,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    logger.info(f"Importing agent from JSON - user: {user_id}")

    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agents currently disabled. This feature is not available at the moment."
    #     )

    client = await db.client
    from .utils import check_agent_count_limit

    limit_check = await check_agent_count_limit(client, user_id)

    if not limit_check["can_create"]:
        error_detail = {
            "message": f"Maximum of {limit_check['limit']} agents allowed for your current plan. You have {limit_check['current_count']} agents.",
            "current_count": limit_check["current_count"],
            "limit": limit_check["limit"],
            "tier_name": limit_check["tier_name"],
            "error_code": "AGENT_LIMIT_EXCEEDED",
        }
        logger.warning(
            f"Agent limit exceeded for account {user_id}: {limit_check['current_count']}/{limit_check['limit']} agents"
        )
        raise HTTPException(status_code=402, detail=error_detail)

    try:
        from agent.json_import_service import JsonImportService, JsonImportRequest

        import_service = JsonImportService(db)

        import_request = JsonImportRequest(
            json_data=request.json_data,
            account_id=user_id,
            instance_name=request.instance_name,
            custom_system_prompt=request.custom_system_prompt,
            profile_mappings=request.profile_mappings,
            custom_mcp_configs=request.custom_mcp_configs,
        )

        result = await import_service.import_json(import_request)

        return JsonImportResponse(
            status=result.status,
            instance_id=result.instance_id,
            name=result.name,
            missing_regular_credentials=result.missing_regular_credentials,
            missing_custom_configs=result.missing_custom_configs,
            agent_info=result.agent_info,
        )

    except Exception as e:
        logger.error(f"Error importing agent from JSON: {str(e)}")
        raise HTTPException(status_code=400, detail=f"Failed to import agent: {str(e)}")


@router.post("/agents", response_model=AgentResponse)
async def create_agent(
    agent_data: AgentCreateRequest, user_id: str = Depends(get_current_user_id_from_jwt)
):
    logger.info(f"Creating new agent for user: {user_id}")
    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agents currently disabled. This feature is not available at the moment."
    #     )

    # 连接数据库
    client = await db.client

    from .utils import check_agent_count_limit

    limit_check = await check_agent_count_limit(client, user_id)

    if not limit_check["can_create"]:
        error_detail = {
            "message": f"Maximum of {limit_check['limit']} agents allowed for your current plan. You have {limit_check['current_count']} agents.",
            "current_count": limit_check["current_count"],
            "limit": limit_check["limit"],
            "tier_name": limit_check["tier_name"],
            "error_code": "AGENT_LIMIT_EXCEEDED",
        }
        logger.warning(
            f"Agent limit exceeded for account {user_id}: {limit_check['current_count']}/{limit_check['limit']} agents"
        )
        raise HTTPException(status_code=402, detail=error_detail)

    try:
        # 创建或更新Agent时，如果 is_default=True,将该用户的所有其他Agent的 is_default 设为 False. 确保只有一个Agent是默认的
        if agent_data.is_default:
            await client.table("agents").eq("user_id", user_id).eq(
                "is_default", True
            ).update({"is_default": False})

        # 获取默认的系统提示词和工具
        from agent.config_helper import get_default_system_prompt_for_fufanmanus_agent

        default_system_prompt = get_default_system_prompt_for_fufanmanus_agent()

        # 获取默认工具配置
        from agent.fufanmanus.config import FufanmanusConfig

        default_tools = FufanmanusConfig.DEFAULT_TOOLS

        insert_data = {
            "agent_id": str(uuid.uuid4()),
            "user_id": user_id,
            "name": agent_data.name,
            "description": agent_data.description or "",
            "system_prompt": agent_data.system_prompt or default_system_prompt,
            "model": agent_data.model or "gpt-4o",
            "configured_mcps": json.dumps(agent_data.configured_mcps or []),
            "custom_mcps": json.dumps(agent_data.custom_mcps or []),
            "agentpress_tools": json.dumps(
                agent_data.agentpress_tools or default_tools
            ),
            "avatar": agent_data.avatar,
            "avatar_color": agent_data.avatar_color,
            "profile_image_url": agent_data.profile_image_url,
            "is_default": agent_data.is_default or False,
            "version_count": 1,
        }

        new_agent = await client.table("agents").insert(insert_data)

        if not new_agent.data:
            raise HTTPException(status_code=500, detail="Failed to create agent")

        agent = new_agent.data[0]

        try:
            version_service = await _get_version_service()

            version = await version_service.create_version(
                agent_id=agent["agent_id"],
                user_id=user_id,
                system_prompt=agent_data.system_prompt or default_system_prompt,
                model=agent_data.model or "gpt-4o",
                configured_mcps=agent_data.configured_mcps or [],
                custom_mcps=agent_data.custom_mcps or [],
                agentpress_tools=agent_data.agentpress_tools or default_tools,
                version_name="v1",
                change_description="Initial version",
            )

            agent["current_version_id"] = version.version_id
            agent["version_count"] = 1

            current_version = AgentVersionResponse(
                version_id=version.version_id,
                agent_id=version.agent_id,
                version_number=version.version_number,
                version_name=version.version_name,
                system_prompt=version.system_prompt,
                model=version.model,
                configured_mcps=version.configured_mcps,
                custom_mcps=version.custom_mcps,
                agentpress_tools=version.agentpress_tools,
                is_active=version.is_active,
                created_at=version.created_at.isoformat(),
                updated_at=version.updated_at.isoformat(),
                created_by=version.created_by,
            )
        except Exception as e:
            logger.error(f"Error creating initial version: {str(e)}")
            await client.table("agents").eq("agent_id", agent["agent_id"]).delete()
            raise HTTPException(
                status_code=500, detail="Failed to create initial version"
            )

        from utils.cache import Cache

        # 清除用户当前Agent数量限制缓存，因为创建了新的Agent，数量发生变化，下次查询时会重新计算
        await Cache.invalidate(f"agent_count_limit:{user_id}")

        logger.info(f"Created agent {agent['agent_id']} with v1 for user: {user_id}")
        return AgentResponse(
            agent_id=agent["agent_id"],
            account_id=agent["user_id"],
            name=agent["name"],
            description=agent.get("description"),
            system_prompt=version.system_prompt,
            model=version.model,
            configured_mcps=version.configured_mcps,
            custom_mcps=version.custom_mcps,
            agentpress_tools=version.agentpress_tools,
            is_default=agent.get("is_default", False),
            is_public=agent.get("is_public", False),
            tags=agent.get("tags", []),
            avatar=agent.get("avatar"),
            avatar_color=agent.get("avatar_color"),
            profile_image_url=agent.get("profile_image_url"),
            created_at=agent["created_at"].isoformat() if agent["created_at"] else None,
            updated_at=(
                agent.get("updated_at").isoformat()
                if agent.get("updated_at")
                else agent["created_at"].isoformat()
            ),
            current_version_id=agent.get("current_version_id"),
            version_count=agent.get("version_count", 1),
            current_version=current_version,
            metadata=(
                json.loads(agent.get("metadata", "{}"))
                if isinstance(agent.get("metadata"), str)
                else agent.get("metadata", {})
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating agent for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to create agent: {str(e)}")


def merge_custom_mcps(
    existing_mcps: List[Dict[str, Any]], new_mcps: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    if not new_mcps:
        return existing_mcps

    merged_mcps = existing_mcps.copy()

    for new_mcp in new_mcps:
        new_mcp_name = new_mcp.get("name")
        existing_index = None

        for i, existing_mcp in enumerate(merged_mcps):
            if existing_mcp.get("name") == new_mcp_name:
                existing_index = i
                break

        if existing_index is not None:
            merged_mcps[existing_index] = new_mcp
        else:
            merged_mcps.append(new_mcp)

    return merged_mcps


@router.put("/agents/{agent_id}", response_model=AgentResponse)
async def update_agent(
    agent_id: str,
    agent_data: AgentUpdateRequest,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agent currently disabled. This feature is not available at the moment."
    #     )
    logger.info(f"Updating agent {agent_id} for user: {user_id}")
    client = await db.client

    try:
        existing_agent = (
            await client.table("agents")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("account_id", user_id)
            .maybe_single()
            .execute()
        )

        if not existing_agent.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        existing_data = existing_agent.data

        agent_metadata = existing_data.get("metadata", {})
        is_fufanmanus_agent = agent_metadata.get("is_fufanmanus_default", False)
        restrictions = agent_metadata.get("restrictions", {})

        if is_fufanmanus_agent:
            logger.warning(
                f"Update attempt on FuFanManus default agent {agent_id} by user {user_id}"
            )

            if (
                agent_data.name is not None
                and agent_data.name != existing_data.get("name")
                and restrictions.get("name_editable") == False
            ):
                logger.error(
                    f"User {user_id} attempted to modify restricted name of FuFanManus agent {agent_id}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="FuFanManus's name cannot be modified. This restriction is managed centrally.",
                )

            if (
                agent_data.description is not None
                and agent_data.description != existing_data.get("description")
                and restrictions.get("description_editable") == False
            ):
                logger.error(
                    f"User {user_id} attempted to modify restricted description of FuFanManus agent {agent_id}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="FuFanManus's description cannot be modified.",
                )

            if (
                agent_data.system_prompt is not None
                and restrictions.get("system_prompt_editable") == False
            ):
                logger.error(
                    f"User {user_id} attempted to modify restricted system prompt of FuFanManus agent {agent_id}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="FuFanManus's system prompt cannot be modified. This is managed centrally to ensure optimal performance.",
                )

            if (
                agent_data.agentpress_tools is not None
                and restrictions.get("tools_editable") == False
            ):
                logger.error(
                    f"User {user_id} attempted to modify restricted tools of FuFanManus agent {agent_id}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="FuFanManus's default tools cannot be modified. These tools are optimized for FuFanManus's capabilities.",
                )

            if (
                agent_data.configured_mcps is not None
                or agent_data.custom_mcps is not None
            ) and restrictions.get("mcps_editable") == False:
                logger.error(
                    f"User {user_id} attempted to modify restricted MCPs of FuFanManus agent {agent_id}"
                )
                raise HTTPException(
                    status_code=403,
                    detail="FuFanManus's integrations cannot be modified.",
                )

            logger.info(
                f"FuFanManus agent update validation passed for agent {agent_id} by user {user_id}"
            )

        current_version_data = None
        if existing_data.get("current_version_id"):
            try:
                version_service = await _get_version_service()
                current_version_obj = await version_service.get_version(
                    agent_id=agent_id,
                    version_id=existing_data["current_version_id"],
                    user_id=user_id,
                )
                current_version_data = current_version_obj.to_dict()
            except Exception as e:
                logger.warning(
                    f"Failed to get current version data for agent {agent_id}: {e}"
                )

        if current_version_data is None:
            logger.info(
                f"Agent {agent_id} has no version data, creating initial version"
            )
            try:
                workflows_result = (
                    await client.table("agent_workflows")
                    .select("*")
                    .eq("agent_id", agent_id)
                    .execute()
                )
                workflows = workflows_result.data if workflows_result.data else []

                initial_version_data = {
                    "agent_id": agent_id,
                    "version_number": 1,
                    "version_name": "v1",
                    "system_prompt": existing_data.get("system_prompt", ""),
                    "configured_mcps": existing_data.get("configured_mcps", []),
                    "custom_mcps": existing_data.get("custom_mcps", []),
                    "agentpress_tools": existing_data.get("agentpress_tools", {}),
                    "is_active": True,
                    "created_by": user_id,
                }

                initial_config = build_unified_config(
                    system_prompt=initial_version_data["system_prompt"],
                    agentpress_tools=initial_version_data["agentpress_tools"],
                    configured_mcps=initial_version_data["configured_mcps"],
                    custom_mcps=initial_version_data["custom_mcps"],
                    avatar=None,
                    avatar_color=None,
                    workflows=workflows,
                )
                initial_version_data["config"] = initial_config

                version_result = (
                    await client.table("agent_versions")
                    .insert(initial_version_data)
                    .execute()
                )

                if version_result.data:
                    version_id = version_result.data[0]["version_id"]

                    await client.table("agents").eq("agent_id", agent_id).update(
                        {"current_version_id": version_id, "version_count": 1}
                    )
                    current_version_data = initial_version_data
                    logger.info(f"Created initial version for agent {agent_id}")
                else:
                    current_version_data = {
                        "system_prompt": existing_data.get("system_prompt", ""),
                        "configured_mcps": existing_data.get("configured_mcps", []),
                        "custom_mcps": existing_data.get("custom_mcps", []),
                        "agentpress_tools": existing_data.get("agentpress_tools", {}),
                    }
            except Exception as e:
                logger.warning(
                    f"Failed to create initial version for agent {agent_id}: {e}"
                )
                current_version_data = {
                    "system_prompt": existing_data.get("system_prompt", ""),
                    "configured_mcps": existing_data.get("configured_mcps", []),
                    "custom_mcps": existing_data.get("custom_mcps", []),
                    "agentpress_tools": existing_data.get("agentpress_tools", {}),
                }

        needs_new_version = False
        version_changes = {}

        def values_different(new_val, old_val):
            if new_val is None:
                return False
            try:
                new_json = (
                    json.dumps(new_val, sort_keys=True) if new_val is not None else None
                )
                old_json = (
                    json.dumps(old_val, sort_keys=True) if old_val is not None else None
                )
                return new_json != old_json
            except (TypeError, ValueError):
                return new_val != old_val

        if values_different(
            agent_data.system_prompt, current_version_data.get("system_prompt")
        ):
            needs_new_version = True
            version_changes["system_prompt"] = agent_data.system_prompt

        if values_different(
            agent_data.configured_mcps, current_version_data.get("configured_mcps", [])
        ):
            needs_new_version = True
            version_changes["configured_mcps"] = agent_data.configured_mcps

        if values_different(
            agent_data.custom_mcps, current_version_data.get("custom_mcps", [])
        ):
            needs_new_version = True
            if agent_data.custom_mcps is not None:
                merged_custom_mcps = merge_custom_mcps(
                    current_version_data.get("custom_mcps", []), agent_data.custom_mcps
                )
                version_changes["custom_mcps"] = merged_custom_mcps
            else:
                version_changes["custom_mcps"] = current_version_data.get(
                    "custom_mcps", []
                )

        if values_different(
            agent_data.agentpress_tools,
            current_version_data.get("agentpress_tools", {}),
        ):
            needs_new_version = True
            version_changes["agentpress_tools"] = agent_data.agentpress_tools

        update_data = {}
        if agent_data.name is not None:
            update_data["name"] = agent_data.name
        if agent_data.description is not None:
            update_data["description"] = agent_data.description
        if agent_data.is_default is not None:
            update_data["is_default"] = agent_data.is_default
            if agent_data.is_default:
                await client.table("agents").eq("user_id", user_id).eq(
                    "is_default", True
                ).neq("agent_id", agent_id).update({"is_default": False})
        if agent_data.avatar is not None:
            update_data["avatar"] = agent_data.avatar
        if agent_data.avatar_color is not None:
            update_data["avatar_color"] = agent_data.avatar_color
        if agent_data.profile_image_url is not None:
            update_data["profile_image_url"] = agent_data.profile_image_url

        current_system_prompt = (
            agent_data.system_prompt
            if agent_data.system_prompt is not None
            else current_version_data.get("system_prompt", "")
        )
        current_configured_mcps = (
            agent_data.configured_mcps
            if agent_data.configured_mcps is not None
            else current_version_data.get("configured_mcps", [])
        )

        if agent_data.custom_mcps is not None:
            current_custom_mcps = merge_custom_mcps(
                current_version_data.get("custom_mcps", []), agent_data.custom_mcps
            )
        else:
            current_custom_mcps = current_version_data.get("custom_mcps", [])

        current_agentpress_tools = (
            agent_data.agentpress_tools
            if agent_data.agentpress_tools is not None
            else current_version_data.get("agentpress_tools", {})
        )
        current_avatar = (
            agent_data.avatar
            if agent_data.avatar is not None
            else existing_data.get("avatar")
        )
        current_avatar_color = (
            agent_data.avatar_color
            if agent_data.avatar_color is not None
            else existing_data.get("avatar_color")
        )
        new_version_id = None
        if needs_new_version:
            try:
                version_service = await _get_version_service()

                new_version = await version_service.create_version(
                    agent_id=agent_id,
                    user_id=user_id,
                    system_prompt=current_system_prompt,
                    configured_mcps=current_configured_mcps,
                    custom_mcps=current_custom_mcps,
                    agentpress_tools=current_agentpress_tools,
                    change_description="Configuration updated",
                )

                new_version_id = new_version.version_id
                update_data["current_version_id"] = new_version_id
                update_data["version_count"] = new_version.version_number

                logger.info(
                    f"Created new version {new_version.version_name} for agent {agent_id}"
                )

            except HTTPException:
                raise
            except Exception as e:
                logger.error(
                    f"Error creating new version for agent {agent_id}: {str(e)}"
                )
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to create new agent version: {str(e)}",
                )

        if update_data:
            try:
                update_result = (
                    await client.table("agents")
                    .eq("agent_id", agent_id)
                    .eq("account_id", user_id)
                    .update(update_data)
                )

                if not update_result.data:
                    raise HTTPException(
                        status_code=500,
                        detail="Failed to update agent - no rows affected",
                    )
            except Exception as e:
                logger.error(f"Error updating agent {agent_id}: {str(e)}")
                raise HTTPException(
                    status_code=500, detail=f"Failed to update agent: {str(e)}"
                )

        updated_agent = (
            await client.table("agents")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("account_id", user_id)
            .maybe_single()
            .execute()
        )

        if not updated_agent.data:
            raise HTTPException(status_code=500, detail="Failed to fetch updated agent")

        agent = updated_agent.data

        current_version = None
        if agent.get("current_version_id"):
            try:
                version_service = await _get_version_service()
                current_version_obj = await version_service.get_version(
                    agent_id=agent_id,
                    version_id=agent["current_version_id"],
                    user_id=user_id,
                )
                current_version_data = current_version_obj.to_dict()
                version_data = current_version_data

                current_version = AgentVersionResponse(
                    version_id=current_version_data["version_id"],
                    agent_id=current_version_data["agent_id"],
                    version_number=current_version_data["version_number"],
                    version_name=current_version_data["version_name"],
                    system_prompt=current_version_data["system_prompt"],
                    model=current_version_data.get("model"),
                    configured_mcps=current_version_data.get("configured_mcps", []),
                    custom_mcps=current_version_data.get("custom_mcps", []),
                    agentpress_tools=current_version_data.get("agentpress_tools", {}),
                    is_active=current_version_data.get("is_active", True),
                    created_at=current_version_data["created_at"],
                    updated_at=current_version_data.get(
                        "updated_at", current_version_data["created_at"]
                    ),
                    created_by=current_version_data.get("created_by"),
                )

                logger.info(
                    f"Using agent {agent['name']} version {current_version_data.get('version_name', 'v1')}"
                )
            except Exception as e:
                logger.warning(
                    f"Failed to get version data for updated agent {agent_id}: {e}"
                )

        version_data = None
        if current_version:
            version_data = {
                "version_id": current_version.version_id,
                "agent_id": current_version.agent_id,
                "version_number": current_version.version_number,
                "version_name": current_version.version_name,
                "system_prompt": current_version.system_prompt,
                "model": current_version.model,
                "configured_mcps": current_version.configured_mcps,
                "custom_mcps": current_version.custom_mcps,
                "agentpress_tools": current_version.agentpress_tools,
                "is_active": current_version.is_active,
            }

        from agent.config_helper import extract_agent_config

        agent_config = extract_agent_config(agent, version_data)

        system_prompt = agent_config["system_prompt"]
        configured_mcps = agent_config["configured_mcps"]
        custom_mcps = agent_config["custom_mcps"]
        agentpress_tools = agent_config["agentpress_tools"]

        return AgentResponse(
            agent_id=agent["agent_id"],
            account_id=agent["user_id"],
            name=agent["name"],
            description=agent.get("description"),
            system_prompt=system_prompt,
            configured_mcps=configured_mcps,
            custom_mcps=custom_mcps,
            agentpress_tools=agentpress_tools,
            is_default=agent.get("is_default", False),
            is_public=agent.get("is_public", False),
            tags=agent.get("tags", []),
            avatar=agent_config.get("avatar"),
            avatar_color=agent_config.get("avatar_color"),
            profile_image_url=agent_config.get("profile_image_url"),
            created_at=agent["created_at"].isoformat() if agent["created_at"] else None,
            updated_at=(
                agent.get("updated_at").isoformat()
                if agent.get("updated_at")
                else agent["created_at"].isoformat()
            ),
            current_version_id=agent.get("current_version_id"),
            version_count=agent.get("version_count", 1),
            current_version=current_version,
            metadata=(
                json.loads(agent.get("metadata", "{}"))
                if isinstance(agent.get("metadata"), str)
                else agent.get("metadata", {})
            ),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent {agent_id} for user {user_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to update agent: {str(e)}")


@router.delete("/agents/{agent_id}")
async def delete_agent(
    agent_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agent currently disabled. This feature is not available at the moment."
    #     )
    logger.info(f"Deleting agent: {agent_id}")
    client = await db.client

    try:
        agent_result = (
            await client.table("agents").select("*").eq("agent_id", agent_id).execute()
        )
        if not agent_result.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent = agent_result.data[0]
        if agent["user_id"] != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        if agent["is_default"]:
            raise HTTPException(status_code=400, detail="Cannot delete default agent")

        if agent.get("metadata", {}).get("is_fufanmanus_default", False):
            raise HTTPException(
                status_code=400, detail="Cannot delete FuFanManus default agent"
            )

        delete_result = await client.table("agents").eq("agent_id", agent_id).delete()

        if not delete_result.data:
            logger.warning(
                f"No agent was deleted for agent_id: {agent_id}, user_id: {user_id}"
            )
            raise HTTPException(
                status_code=403,
                detail="Unable to delete agent - permission denied or agent not found",
            )

        try:
            from utils.cache import Cache

            await Cache.invalidate(f"agent_count_limit:{user_id}")
        except Exception as cache_error:
            logger.warning(
                f"Cache invalidation failed for user {user_id}: {str(cache_error)}"
            )

        logger.info(f"Successfully deleted agent: {agent_id}")
        return {"message": "Agent deleted successfully"}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting agent {agent_id}: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/agents/{agent_id}/builder-chat-history")
async def get_agent_builder_chat_history(
    agent_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(
    #         status_code=403,
    #         detail="Custom agents currently disabled. This feature is not available at the moment."
    #     )

    logger.info(f"Fetching agent builder chat history for agent: {agent_id}")
    client = await db.client

    try:
        agent_result = (
            await client.table("agents")
            .select("*")
            .eq("agent_id", agent_id)
            .eq("user_id", user_id)
            .execute()
        )
        if not agent_result.data:
            raise HTTPException(
                status_code=404, detail="Agent not found or access denied"
            )

        threads_result = (
            await client.table("threads")
            .select("thread_id, created_at, metadata")
            .eq("account_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )

        agent_builder_threads = []
        for thread in threads_result.data:
            metadata = thread.get("metadata", {})
            # 如果metadata是字符串，解析为字典
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (json.JSONDecodeError, TypeError):
                    metadata = {}
            elif not isinstance(metadata, dict):
                metadata = {}

            if (
                metadata.get("is_agent_builder")
                and metadata.get("target_agent_id") == agent_id
            ):
                agent_builder_threads.append(
                    {
                        "thread_id": thread["thread_id"],
                        "created_at": thread["created_at"],
                    }
                )

        if not agent_builder_threads:
            logger.info(f"No agent builder threads found for agent {agent_id}")
            return {"messages": [], "thread_id": None}

        latest_thread_id = agent_builder_threads[0]["thread_id"]
        logger.info(
            f"Found {len(agent_builder_threads)} agent builder threads, using latest: {latest_thread_id}"
        )
        # 从ADK events表查询消息（按时间排序）
        messages_result = (
            await client.schema("public")
            .table("events")
            .select("*")
            .eq("session_id", latest_thread_id)
            .order("timestamp", desc=False)
            .execute()
        )

        logger.info(
            f"Found {len(messages_result.data)} events for agent builder chat history"
        )

        # 转换ADK events为消息格式
        messages = _convert_adk_events_to_messages(messages_result.data)

        return {"messages": messages, "thread_id": latest_thread_id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(
            f"Error fetching agent builder chat history for agent {agent_id}: {str(e)}"
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch chat history: {str(e)}"
        )


@router.get("/agents/{agent_id}/pipedream-tools/{profile_id}")
async def get_pipedream_tools_for_agent(
    agent_id: str,
    profile_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
    version: Optional[str] = Query(
        None, description="Version ID to get tools from specific version"
    ),
):
    logger.info(
        f"Getting tools for agent {agent_id}, profile {profile_id}, user {user_id}, version {version}"
    )

    try:
        from pipedream import profile_service, mcp_service
        from uuid import UUID

        profile = await profile_service.get_profile(UUID(user_id), UUID(profile_id))

        if not profile:
            logger.error(f"Profile {profile_id} not found for user {user_id}")
            try:
                all_profiles = await profile_service.get_profiles(UUID(user_id))
                pipedream_profiles = [
                    p for p in all_profiles if "pipedream" in p.mcp_qualified_name
                ]
                logger.info(
                    f"User {user_id} has {len(pipedream_profiles)} pipedream profiles: {[p.profile_id for p in pipedream_profiles]}"
                )
            except Exception as debug_e:
                logger.warning(f"Could not check user's profiles: {str(debug_e)}")

            raise HTTPException(
                status_code=404,
                detail=f"Profile {profile_id} not found or access denied",
            )

        if not profile.is_connected:
            raise HTTPException(status_code=400, detail="Profile is not connected")

        enabled_tools = []
        try:
            client = await db.client
            agent_row = (
                await client.table("agents")
                .select("current_version_id")
                .eq("agent_id", agent_id)
                .eq("account_id", user_id)
                .maybe_single()
                .execute()
            )

            if agent_row.data and agent_row.data.get("current_version_id"):
                if version:
                    version_result = (
                        await client.table("agent_versions")
                        .select("config")
                        .eq("version_id", version)
                        .maybe_single()
                        .execute()
                    )
                else:
                    version_result = (
                        await client.table("agent_versions")
                        .select("config")
                        .eq("version_id", agent_row.data["current_version_id"])
                        .maybe_single()
                        .execute()
                    )

                if version_result.data and version_result.data.get("config"):
                    agent_config = version_result.data["config"]
                    tools = agent_config.get("tools", {})
                    custom_mcps = tools.get("custom_mcp", []) or []

                    for mcp in custom_mcps:
                        mcp_profile_id = mcp.get("config", {}).get("profile_id")
                        if mcp_profile_id == profile_id:
                            enabled_tools = mcp.get(
                                "enabledTools", mcp.get("enabled_tools", [])
                            )
                            logger.info(
                                f"Found enabled tools for profile {profile_id}: {enabled_tools}"
                            )
                            break

                    if not enabled_tools:
                        logger.info(
                            f"No enabled tools found for profile {profile_id} in agent {agent_id}"
                        )

        except Exception as e:
            logger.error(
                f"Error retrieving enabled tools for profile {profile_id}: {str(e)}"
            )

        logger.info(
            f"Using {len(enabled_tools)} enabled tools for profile {profile_id}: {enabled_tools}"
        )

        try:
            from pipedream.mcp_service import ExternalUserId, AppSlug

            external_user_id = ExternalUserId(profile.external_user_id)
            app_slug_obj = AppSlug(profile.app_slug)

            logger.info(
                f"Discovering servers for user {external_user_id.value} and app {app_slug_obj.value}"
            )
            servers = await mcp_service.discover_servers_for_user(
                external_user_id, app_slug_obj
            )
            logger.info(
                f"Found {len(servers)} servers: {[s.app_slug for s in servers]}"
            )

            server = servers[0] if servers else None
            logger.info(
                f"Selected server: {server.app_slug if server else 'None'} with {len(server.available_tools) if server else 0} tools"
            )

            if not server:
                return {
                    "profile_id": profile_id,
                    "app_name": profile.app_name,
                    "profile_name": profile.profile_name,
                    "tools": [],
                    "has_mcp_config": len(enabled_tools) > 0,
                }

            available_tools = server.available_tools

            formatted_tools = []

            def tools_match(api_tool_name, stored_tool_name):
                api_normalized = api_tool_name.lower().replace("-", "_")
                stored_normalized = stored_tool_name.lower().replace("-", "_")
                return api_normalized == stored_normalized

            for tool in available_tools:
                is_enabled = any(
                    tools_match(tool.name, stored_tool) for stored_tool in enabled_tools
                )
                formatted_tools.append(
                    {
                        "name": tool.name,
                        "description": tool.description
                        or f"Tool from {profile.app_name}",
                        "enabled": is_enabled,
                    }
                )

            return {
                "profile_id": profile_id,
                "app_name": profile.app_name,
                "profile_name": profile.profile_name,
                "tools": formatted_tools,
                "has_mcp_config": len(enabled_tools) > 0,
            }

        except Exception as e:
            logger.error(f"Error discovering tools: {e}", exc_info=True)
            return {
                "profile_id": profile_id,
                "app_name": getattr(profile, "app_name", "Unknown"),
                "profile_name": getattr(profile, "profile_name", "Unknown"),
                "tools": [],
                "has_mcp_config": len(enabled_tools) > 0,
                "error": str(e),
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting Pipedream tools for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/agents/{agent_id}/pipedream-tools/{profile_id}")
async def update_pipedream_tools_for_agent(
    agent_id: str,
    profile_id: str,
    request: dict,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    try:
        client = await db.client
        agent_row = (
            await client.table("agents")
            .select("current_version_id")
            .eq("agent_id", agent_id)
            .eq("account_id", user_id)
            .maybe_single()
            .execute()
        )
        if not agent_row.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent_config = {}
        if agent_row.data.get("current_version_id"):
            version_result = (
                await client.table("agent_versions")
                .select("config")
                .eq("version_id", agent_row.data["current_version_id"])
                .maybe_single()
                .execute()
            )
            if version_result.data and version_result.data.get("config"):
                agent_config = version_result.data["config"]

        tools = agent_config.get("tools", {})
        custom_mcps = tools.get("custom_mcp", []) or []

        if any(
            mcp.get("config", {}).get("profile_id") == profile_id for mcp in custom_mcps
        ):
            raise HTTPException(
                status_code=400, detail="This profile is already added to this agent"
            )

        enabled_tools = request.get("enabled_tools", [])

        updated = False
        for mcp in custom_mcps:
            mcp_profile_id = mcp.get("config", {}).get("profile_id")
            if mcp_profile_id == profile_id:
                mcp["enabledTools"] = enabled_tools
                mcp["enabled_tools"] = enabled_tools
                updated = True
                logger.info(
                    f"Updated enabled tools for profile {profile_id}: {enabled_tools}"
                )
                break

        if not updated:
            logger.warning(
                f"Profile {profile_id} not found in agent {agent_id} custom_mcps configuration"
            )

        if updated:
            agent_config["tools"]["custom_mcp"] = custom_mcps

            await client.table("agent_versions").eq(
                "version_id", agent_row.data["current_version_id"]
            ).update({"config": agent_config})

            logger.info(f"Successfully updated agent configuration for {agent_id}")

        result = {
            "success": updated,
            "enabled_tools": enabled_tools,
            "total_tools": len(enabled_tools),
            "profile_id": profile_id,
        }
        logger.info(
            f"Successfully updated Pipedream tools for agent {agent_id}, profile {profile_id}"
        )
        return result

    except ValueError as e:
        logger.error(f"Validation error updating Pipedream tools: {e}")
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating Pipedream tools for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/agents/{agent_id}/custom-mcp-tools")
async def get_custom_mcp_tools_for_agent(
    agent_id: str,
    request: Request,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    logger.info(f"Getting custom MCP tools for agent {agent_id}, user {user_id}")
    try:
        client = await db.client
        agent_result = (
            await client.table("agents")
            .select("current_version_id")
            .eq("agent_id", agent_id)
            .eq("account_id", user_id)
            .execute()
        )
        if not agent_result.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent = agent_result.data[0]

        agent_config = {}
        if agent.get("current_version_id"):
            version_result = (
                await client.table("agent_versions")
                .select("config")
                .eq("version_id", agent["current_version_id"])
                .maybe_single()
                .execute()
            )
            if version_result.data and version_result.data.get("config"):
                agent_config = version_result.data["config"]

        tools = agent_config.get("tools", {})
        custom_mcps = tools.get("custom_mcp", [])

        mcp_url = request.headers.get("X-MCP-URL")
        mcp_type = request.headers.get("X-MCP-Type", "sse")

        if not mcp_url:
            raise HTTPException(status_code=400, detail="X-MCP-URL header is required")

        mcp_config = {"url": mcp_url, "type": mcp_type}

        if "X-MCP-Headers" in request.headers:
            try:
                mcp_config["headers"] = json.loads(request.headers["X-MCP-Headers"])
            except json.JSONDecodeError:
                logger.warning("Failed to parse X-MCP-Headers as JSON")

        from mcp_module import mcp_service

        discovery_result = await mcp_service.discover_custom_tools(mcp_type, mcp_config)

        existing_mcp = None
        for mcp in custom_mcps:
            if (
                mcp.get("type") == mcp_type
                and mcp.get("config", {}).get("url") == mcp_url
            ):
                existing_mcp = mcp
                break

        tools = []
        enabled_tools = existing_mcp.get("enabledTools", []) if existing_mcp else []

        for tool in discovery_result.tools:
            tools.append(
                {
                    "name": tool["name"],
                    "description": tool.get(
                        "description", f"Tool from {mcp_type.upper()} MCP server"
                    ),
                    "enabled": tool["name"] in enabled_tools,
                }
            )

        return {
            "tools": tools,
            "has_mcp_config": existing_mcp is not None,
            "server_type": mcp_type,
            "server_url": mcp_url,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting custom MCP tools for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/agents/{agent_id}/custom-mcp-tools")
async def update_custom_mcp_tools_for_agent(
    agent_id: str, request: dict, user_id: str = Depends(get_current_user_id_from_jwt)
):
    logger.info(f"Updating custom MCP tools for agent {agent_id}, user {user_id}")

    try:
        client = await db.client

        agent_result = (
            await client.table("agents")
            .select("current_version_id")
            .eq("agent_id", agent_id)
            .eq("account_id", user_id)
            .execute()
        )
        if not agent_result.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent = agent_result.data[0]

        agent_config = {}
        if agent.get("current_version_id"):
            version_result = (
                await client.table("agent_versions")
                .select("config")
                .eq("version_id", agent["current_version_id"])
                .maybe_single()
                .execute()
            )
            if version_result.data and version_result.data.get("config"):
                agent_config = version_result.data["config"]

        tools = agent_config.get("tools", {})
        custom_mcps = tools.get("custom_mcp", [])

        mcp_url = request.get("url")
        mcp_type = request.get("type", "sse")
        enabled_tools = request.get("enabled_tools", [])

        if not mcp_url:
            raise HTTPException(status_code=400, detail="MCP URL is required")

        updated = False
        for i, mcp in enumerate(custom_mcps):
            if mcp_type == "composio":
                # For Composio, match by profile_id
                if (
                    mcp.get("type") == "composio"
                    and mcp.get("config", {}).get("profile_id") == mcp_url
                ):
                    custom_mcps[i]["enabledTools"] = enabled_tools
                    updated = True
                    break
            else:
                if (
                    mcp.get("customType") == mcp_type
                    and mcp.get("config", {}).get("url") == mcp_url
                ):
                    custom_mcps[i]["enabledTools"] = enabled_tools
                    updated = True
                    break

        if not updated:
            if mcp_type == "composio":
                try:
                    from composio_integration.composio_profile_service import (
                        ComposioProfileService,
                    )
                    from services.postgresql import DBConnection

                    profile_service = ComposioProfileService(DBConnection())

                    profile_id = mcp_url
                    mcp_config = await profile_service.get_mcp_config_for_agent(
                        profile_id
                    )
                    mcp_config["enabledTools"] = enabled_tools
                    custom_mcps.append(mcp_config)
                except Exception as e:
                    logger.error(f"Failed to get Composio profile config: {e}")
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to get Composio profile: {str(e)}",
                    )
            else:
                new_mcp_config = {
                    "name": f"Custom MCP ({mcp_type.upper()})",
                    "customType": mcp_type,
                    "type": mcp_type,
                    "config": {"url": mcp_url},
                    "enabledTools": enabled_tools,
                }
                custom_mcps.append(new_mcp_config)

        tools["custom_mcp"] = custom_mcps
        agent_config["tools"] = tools

        from agent.versioning.version_service import get_version_service

        try:
            version_service = await get_version_service()
            new_version = await version_service.create_version(
                agent_id=agent_id,
                user_id=user_id,
                system_prompt=agent_config.get("system_prompt", ""),
                configured_mcps=agent_config.get("tools", {}).get("mcp", []),
                custom_mcps=custom_mcps,
                agentpress_tools=agent_config.get("tools", {}).get("agentpress", {}),
                change_description=f"Updated custom MCP tools for {mcp_type}",
            )
            logger.info(
                f"Created version {new_version.version_id} for custom MCP tools update on agent {agent_id}"
            )
        except Exception as e:
            logger.error(f"Failed to create version for custom MCP tools update: {e}")
            raise HTTPException(status_code=500, detail="Failed to save changes")

        return {
            "success": True,
            "enabled_tools": enabled_tools,
            "total_tools": len(enabled_tools),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating custom MCP tools for agent {agent_id}: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/agents/{agent_id}/tools")
async def get_agent_tools(
    agent_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    # 🔧 暂时禁用功能开关检查
    # if not await is_enabled("custom_agents"):
    #     raise HTTPException(status_code=403, detail="Custom agents currently disabled")

    logger.info(f"Fetching enabled tools for agent: {agent_id} by user: {user_id}")
    client = await db.client

    agent_result = (
        await client.table("agents").select("*").eq("agent_id", agent_id).execute()
    )
    if not agent_result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = agent_result.data[0]
    if agent["user_id"] != user_id and not agent.get("is_public", False):
        raise HTTPException(status_code=403, detail="Access denied")

    version_data = None
    if agent.get("current_version_id"):
        try:
            version_service = await _get_version_service()

            version_obj = await version_service.get_version(
                agent_id=agent_id,
                version_id=agent["current_version_id"],
                user_id=user_id,
            )
            version_data = version_obj.to_dict()
        except Exception as e:
            logger.warning(f"Failed to fetch version data for tools endpoint: {e}")

    from agent.config_helper import extract_agent_config

    agent_config = extract_agent_config(agent, version_data)

    agentpress_tools_config = agent_config["agentpress_tools"]
    configured_mcps = agent_config["configured_mcps"]
    custom_mcps = agent_config["custom_mcps"]

    agentpress_tools = []
    for name, enabled in agentpress_tools_config.items():
        is_enabled_tool = (
            bool(enabled.get("enabled", False))
            if isinstance(enabled, dict)
            else bool(enabled)
        )
        agentpress_tools.append({"name": name, "enabled": is_enabled_tool})

    mcp_tools = []
    for mcp in configured_mcps + custom_mcps:
        server = mcp.get("name")
        enabled_tools = mcp.get("enabledTools") or mcp.get("enabled_tools") or []
        for tool_name in enabled_tools:
            mcp_tools.append({"name": tool_name, "server": server, "enabled": True})
    return {"agentpress_tools": agentpress_tools, "mcp_tools": mcp_tools}


@router.get("/threads")
async def get_user_threads(
    user_id: str = Depends(get_current_user_id_from_jwt),
    page: Optional[int] = Query(1, ge=1, description="Page number (1-based)"),
    limit: Optional[int] = Query(
        1000, ge=1, le=1000, description="Number of items per page (max 1000)"
    ),
):
    """获取当前用户的所有对话线程，包含关联的项目数据"""
    logger.info(
        f"Fetching threads with project data for user: {user_id} (page={page}, limit={limit})"
    )
    client = await db.client
    try:
        # 计算分页偏移量
        offset = (page - 1) * limit

        # 步骤1: 从threads表中获取指定用户的所有对话线程，按创建时间倒序排列
        threads_result = (
            await client.table("threads")
            .select("*")
            .eq("account_id", user_id)
            .order("created_at", desc=True)
            .execute()
        )

        # 如果没有找到任何线程，返回空结果
        if not threads_result.data:
            logger.info(f"No threads found for user: {user_id}")
            return {
                "threads": [],
                "pagination": {"page": page, "limit": limit, "total": 0, "pages": 0},
            }

        # 获取总线程数量
        total_count = len(threads_result.data)

        # 步骤2: 对线程数据进行分页处理
        paginated_threads = threads_result.data[offset : offset + limit]

        # 步骤3: 提取所有线程中关联的项目ID，并去重
        project_ids = [
            thread["project_id"]
            for thread in paginated_threads
            if thread.get("project_id")
        ]
        unique_project_ids = list(set(project_ids)) if project_ids else []

        # 步骤4: 如果有项目ID，则批量获取项目数据
        projects_by_id = {}
        if unique_project_ids:
            projects_result = (
                await client.table("projects")
                .select("*")
                .in_("project_id", unique_project_ids)
                .execute()
            )

            if projects_result.data:
                logger.info(f"[API] Raw projects from DB: {len(projects_result.data)}")
                # 创建项目ID到项目数据的映射表，便于快速查找
                projects_by_id = {
                    project["project_id"]: project for project in projects_result.data
                }

        # 步骤5: 将线程数据与项目数据进行关联映射
        mapped_threads = []
        for thread in paginated_threads:
            project_data = None
            # 如果线程有关联的项目，则获取项目数据
            if thread.get("project_id") and thread["project_id"] in projects_by_id:
                project = projects_by_id[thread["project_id"]]
                project_data = {
                    "project_id": project["project_id"],
                    "name": project.get("name", ""),
                    "description": project.get("description", ""),
                    "account_id": project["account_id"],
                    "sandbox": project.get("sandbox", {}),
                    "is_public": project.get("is_public", False),
                    "created_at": project["created_at"],
                    "updated_at": project["updated_at"],
                }

            # 构建线程数据结构，包含关联的项目信息
            mapped_thread = {
                "thread_id": thread["thread_id"],
                "account_id": thread["account_id"],
                "project_id": thread.get("project_id"),
                "metadata": thread.get("metadata", {}),
                "is_public": thread.get("is_public", False),
                "created_at": thread["created_at"],
                "updated_at": thread["updated_at"],
                "project": project_data,  # 关联的项目数据
            }
            mapped_threads.append(mapped_thread)

        # 步骤6: 计算总页数
        total_pages = (total_count + limit - 1) // limit if total_count else 0

        logger.info(
            f"[API] Mapped threads for frontend: {len(mapped_threads)} threads, {len(projects_by_id)} unique projects"
        )

        # 步骤7: 返回结果，包含线程列表和分页信息
        return {
            "threads": mapped_threads,
            "pagination": {
                "page": page,
                "limit": limit,
                "total": total_count,
                "pages": total_pages,
            },
        }

    except Exception as e:
        logger.error(f"Error fetching threads for user {user_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch threads: {str(e)}"
        )


@router.get("/projects/{project_id}")
async def get_project(
    project_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Get a specific project by ID with complete related data."""
    logger.info(f"Fetching project: {project_id}")
    client = await db.client

    try:

        # 正常查询，sandbox字段会自动处理为JSON字符串
        project_result = (
            await client.table("projects")
            .select("*")
            .eq("project_id", project_id)
            .execute()
        )

        if not project_result.data:
            raise HTTPException(status_code=404, detail="Project not found")

        project = project_result.data[0]

        # 验证项目访问权限
        if project.get("account_id") != user_id:
            raise HTTPException(status_code=403, detail="Access denied")

        # 获取项目关联的线程

        threads_result = (
            await client.table("threads")
            .select("*")
            .eq("project_id", project_id)
            .order("created_at", desc=True)
            .execute()
        )
        threads_data = []
        if threads_result.data:
            threads_data = [
                {
                    "thread_id": thread["thread_id"],
                    "account_id": thread["account_id"],
                    "metadata": thread.get("metadata", {}),
                    "is_public": thread.get("is_public", False),
                    "created_at": thread["created_at"],
                    "updated_at": thread["updated_at"],
                }
                for thread in threads_result.data
            ]

            # 打印最近的几个线程
            for i, thread in enumerate(threads_result.data[:3]):  # 只显示前3个
                logger.info(
                    f"{i+1}. thread_id: {thread['thread_id']}, created_at: {thread['created_at']}"
                )
        else:
            logger.info(f"not found threads")

        # 获取项目相关的Agent运行记录
        agent_runs_result = (
            await client.table("agent_runs")
            .select("*")
            .in_("thread_id", [t["thread_id"] for t in threads_data])
            .order("created_at", desc=True)
            .execute()
        )
        agent_runs_data = []
        if agent_runs_result.data:
            agent_runs_data = [
                {
                    "id": run["id"],
                    "thread_id": run["thread_id"],
                    "status": run.get("status", ""),
                    "started_at": run.get("started_at"),
                    "completed_at": run.get("completed_at"),
                    "error": run.get("error"),
                    "agent_id": run.get("agent_id"),
                    "agent_version_id": run.get("agent_version_id"),
                    "created_at": run["created_at"],
                }
                for run in agent_runs_result.data
            ]

            # 打印最近的几条运行记录
            for i, run in enumerate(agent_runs_result.data[:3]):  # 只显示前3条
                logger.info(
                    f"      {i+1}. ID: {run['id']}, 状态: {run.get('status', 'N/A')}, 线程: {run.get('thread_id')}"
                )
        else:
            logger.info(f"not found agent runs")

        # 统计项目总消息数

        total_message_count = 0
        total_visible_message_count = 0
        if threads_data:
            for thread in threads_data:
                message_count_result = (
                    await client.schema("public")
                    .table("events")
                    .select("id", count="exact")
                    .eq("session_id", thread["thread_id"])
                    .execute()
                )
                thread_message_count = (
                    message_count_result.count
                    if message_count_result.count is not None
                    else 0
                )
                total_message_count += thread_message_count
                total_visible_message_count += (
                    await _fetch_thread_visible_message_count(
                        client, thread["thread_id"]
                    )
                )

        # 处理sandbox信息 - 从测试脚本知道sandbox字段是JSON字符串
        raw_sandbox = project.get("sandbox", "{}")

        if isinstance(raw_sandbox, str):
            try:
                import json

                sandbox_info = json.loads(raw_sandbox) if raw_sandbox.strip() else {}
            except json.JSONDecodeError as e:
                sandbox_info = {}
        elif isinstance(raw_sandbox, dict):
            sandbox_info = raw_sandbox
        else:
            sandbox_info = {}

        mapped_project = {
            "project_id": project["project_id"],
            "name": project.get("name", ""),
            "description": project.get("description", ""),
            "account_id": project["account_id"],
            "sandbox": sandbox_info,
            "is_public": project.get("is_public", False),
            "created_at": project["created_at"],
            "updated_at": project["updated_at"],
            "threads": threads_data,
            "agent_runs": agent_runs_data,
            "total_message_count": total_message_count,
            "total_visible_message_count": total_visible_message_count,
            "thread_count": len(threads_data),
        }

        logger.info(
            f"[API] Mapped project for frontend: {project_id} with {len(threads_data)} threads and {total_message_count} total messages"
        )

        return mapped_project

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch project: {str(e)}")
        logger.error(f"Error fetching project {project_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch project: {str(e)}"
        )


@router.get("/threads/{thread_id}")
async def get_thread(
    thread_id: str, user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Get a specific thread by ID with complete related data."""
    logger.info(f"Fetching thread: {thread_id}")
    client = await db.client

    try:
        await verify_thread_access(client, thread_id, user_id)

        # Get the thread data
        thread_result = (
            await client.table("threads")
            .select("*")
            .eq("thread_id", thread_id)
            .execute()
        )

        if not thread_result.data:

            raise HTTPException(status_code=404, detail="Thread not found")

        thread = thread_result.data[0]

        # Get associated project if thread has a project_id

        project_data = None
        if thread.get("project_id"):

            project_result = (
                await client.table("projects")
                .select("*")
                .eq("project_id", thread["project_id"])
                .execute()
            )

            if project_result.data:
                project = project_result.data[0]

                logger.info(f"[API] Raw project from DB for thread {thread_id}")
                project_data = {
                    "project_id": project["project_id"],
                    "name": project.get("name", ""),
                    "description": project.get("description", ""),
                    "account_id": project["account_id"],
                    "sandbox": project.get("sandbox", {}),
                    "is_public": project.get("is_public", False),
                    "created_at": project["created_at"],
                    "updated_at": project["updated_at"],
                }
            else:
                logger.error(f"project not found: {thread['project_id']}")
        else:
            logger.info(f"thread not found project")

        # Get message count for the thread
        # 从ADK events表统计消息数量
        message_count_result = (
            await client.schema("public")
            .table("events")
            .select("id", count="exact")
            .eq("session_id", thread_id)
            .execute()
        )
        message_count = (
            message_count_result.count if message_count_result.count is not None else 0
        )
        visible_message_count = await _fetch_thread_visible_message_count(
            client, thread_id
        )

        # Get recent agent runs for the thread
        agent_runs_result = (
            await client.table("agent_runs")
            .select("*")
            .eq("thread_id", thread_id)
            .order("created_at", desc=True)
            .execute()
        )
        agent_runs_data = []
        if agent_runs_result.data:
            agent_runs_data = [
                {
                    "id": run["id"],
                    "status": run.get("status", ""),
                    "started_at": run.get("started_at"),
                    "completed_at": run.get("completed_at"),
                    "error": run.get("error"),
                    "agent_id": run.get("agent_id"),
                    "agent_version_id": run.get("agent_version_id"),
                    "created_at": run["created_at"],
                }
                for run in agent_runs_result.data
            ]

            # 打印最近的几条运行记录
            for i, run in enumerate(agent_runs_result.data[:3]):  # 只显示前3条
                logger.info(
                    f"      {i+1}. ID: {run['id']}, status: {run.get('status', 'N/A')}, started_at: {run.get('started_at')}"
                )
        else:
            logger.info(f"not found agent runs")

        # Map thread data for frontend (matching actual DB structure)
        mapped_thread = {
            "thread_id": thread["thread_id"],
            "account_id": thread["account_id"],
            "project_id": thread.get("project_id"),
            "metadata": thread.get("metadata", {}),
            "is_public": thread.get("is_public", False),
            "created_at": thread["created_at"],
            "updated_at": thread["updated_at"],
            "project": project_data,
            "message_count": message_count,
            "visible_message_count": visible_message_count,
            "recent_agent_runs": agent_runs_data,
        }

        logger.info(
            f"[API] Mapped thread for frontend: {thread_id} with {message_count} messages and {len(agent_runs_data)} recent runs"
        )
        return mapped_thread

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to fetch thread: {str(e)}")
        logger.error(f"Error fetching thread {thread_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to fetch thread: {str(e)}")


@router.post("/threads", response_model=CreateThreadResponse)
async def create_thread(
    name: Optional[str] = Form(None),
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """
    Create a new thread without starting an agent run.

    [WARNING] Keep in sync with initiate endpoint.
    """
    if not name:
        name = "New Project"
    logger.info(f"Creating new thread with name: {name}")
    client = await db.client
    account_id = user_id  # In Basejump, personal account_id is the same as user_id

    try:
        # 1. Create Project
        project_name = name or "New Project"
        project = (
            await client.schema("public")
            .table("projects")
            .insert(
                {
                    "project_id": str(uuid.uuid4()),
                    "account_id": account_id,
                    "name": project_name,
                    "created_at": datetime.now(),
                }
            )
        )
        project_id = project.data[0]["project_id"]
        logger.info(f"Created new project: {project_id}")

        # 2. Create Sandbox
        sandbox_id = None
        try:
            sandbox_pass = str(uuid.uuid4())
            sandbox = await create_sandbox(sandbox_pass, project_id)
            sandbox_id = sandbox.sandbox_id
            logger.info(f"Created new sandbox {sandbox_id} for project {project_id}")

            # Get preview links via PPIO get_host()
            vnc_host = await asyncio.to_thread(sandbox.get_host, 6080)
            website_host = await asyncio.to_thread(sandbox.get_host, 8080)
            vnc_url = f"https://{vnc_host}"
            website_url = f"https://{website_host}"
            token = None
        except Exception as e:
            logger.error(f"Error creating sandbox: {str(e)}")
            await client.table("projects").eq("project_id", project_id).delete()
            if sandbox_id:
                try:
                    await delete_sandbox(sandbox_id)
                except Exception as e:
                    logger.error(f"Error deleting sandbox: {str(e)}")
            raise Exception("Failed to create sandbox")

        # Update project with sandbox info
        update_result = (
            await client.table("projects")
            .eq("project_id", project_id)
            .update(
                {
                    "sandbox": json.dumps(
                        {
                            "id": sandbox_id,
                            "pass": sandbox_pass,
                            "vnc_preview": vnc_url,
                            "sandbox_url": website_url,
                            "token": token,
                        }
                    )
                }
            )
        )

        if not update_result.data:
            logger.error(
                f"Failed to update project {project_id} with new sandbox {sandbox_id}"
            )
            if sandbox_id:
                try:
                    await delete_sandbox(sandbox_id)
                except Exception as e:
                    logger.error(f"Error deleting sandbox: {str(e)}")
            raise Exception("Database update failed")

        # 3. Create Thread
        thread_data = {
            "thread_id": str(uuid.uuid4()),
            "project_id": project_id,
            "account_id": account_id,
            "created_at": datetime.now(),
        }

        structlog.contextvars.bind_contextvars(
            thread_id=thread_data["thread_id"],
            project_id=project_id,
            account_id=account_id,
        )

        thread = await client.schema("public").table("threads").insert(thread_data)
        thread_id = thread.data[0]["thread_id"]
        logger.info(f"Created new thread: {thread_id}")

        await _create_adk_session_if_not_exists(client, user_id, thread_id)

        logger.info(
            f"Successfully created thread {thread_id} with project {project_id}"
        )
        return {"thread_id": thread_id, "project_id": project_id}

    except Exception as e:
        logger.error(f"Error creating thread: {str(e)}\n{traceback.format_exc()}")
        # TODO: Clean up created project/thread if creation fails mid-way
        raise HTTPException(
            status_code=500, detail=f"Failed to create thread: {str(e)}"
        )


@router.get("/threads/{thread_id}/messages")
async def get_thread_messages(
    thread_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
    order: str = Query("desc", description="Order by created_at: 'asc' or 'desc'"),
):
    """Get all messages for a thread, fetching in batches of 1000 from the DB to avoid large queries."""
    logger.info(f"Fetching all messages for thread: {thread_id}, order={order}")
    client = await db.client
    await verify_thread_access(client, thread_id, user_id)
    try:
        batch_size = 1000
        offset = 0
        all_messages = []
        # 🔧 Step 1: 从 messages 表查询系统消息 (assistant, tool, status等)
        all_system_messages = []
        offset = 0
        while True:
            query = client.table("messages").select("*").eq("thread_id", thread_id)
            query = query.order("created_at", desc=(order == "desc"))
            query = query.range(offset, offset + batch_size - 1)
            messages_result = await query.execute()
            batch = messages_result.data or []
            all_system_messages.extend(batch)
            logger.debug(
                f"Fetched batch of {len(batch)} system messages (offset {offset})"
            )
            if len(batch) < batch_size:
                break
            offset += batch_size

        # 🔧 Step 2: 从 events 表查询用户消息与 assistant fallback 消息
        all_user_events = []
        all_assistant_events = []
        for author, target_events in (
            ("user", all_user_events),
            ("assistant", all_assistant_events),
        ):
            offset = 0
            while True:
                query = (
                    client.schema("public")
                    .table("events")
                    .select("*")
                    .eq("session_id", thread_id)
                    .eq("author", author)
                )
                query = query.order("timestamp", desc=(order == "desc"))
                query = query.range(offset, offset + batch_size - 1)
                events_result = await query.execute()
                batch = events_result.data or []
                target_events.extend(batch)
                logger.debug(
                    f"Fetched batch of {len(batch)} {author} events (offset {offset})"
                )
                if len(batch) < batch_size:
                    break
                offset += batch_size

        # 🔧 Step 3: 转换并合并 canonical messages + events fallback
        user_messages = _convert_user_events_to_messages(all_user_events)
        if user_messages:
            # Avoid duplicate user messages when both tables contain user entries.
            all_system_messages = [
                msg for msg in all_system_messages if msg.get("type") != "user"
            ]
        system_messages = _format_messages_from_table(all_system_messages)
        assistant_event_messages = _convert_assistant_events_to_messages(
            all_assistant_events
        )
        assistant_fallback_messages = _dedupe_event_fallback_messages(
            table_messages=system_messages,
            fallback_messages=assistant_event_messages,
        )
        shadow_clone_v2_final_messages = _dedupe_event_fallback_messages(
            table_messages=system_messages + assistant_fallback_messages,
            fallback_messages=await _convert_shadow_clone_v2_final_outputs_to_messages(
                client,
                thread_id,
            ),
        )

        # 🔧 Step 4: 合并并按时间排序
        all_messages = (
            system_messages
            + user_messages
            + assistant_fallback_messages
            + shadow_clone_v2_final_messages
        )
        all_messages.sort(
            key=lambda x: _normalize_timestamp_value(x.get("created_at")),
            reverse=(order == "desc"),
        )

        logger.info(
            "thread messages diagnostic: "
            f"thread_id={thread_id}, "
            f"messages_table_by_type={_count_by_field(all_system_messages, 'type')}, "
            f"events_by_author={_count_by_field(all_user_events + all_assistant_events, 'author')}, "
            f"returned_by_type={_count_by_field(all_messages, 'type')}, "
            f"latest_assistant={_latest_assistant_info(all_messages)}"
        )

        return {"messages": all_messages}
    except Exception as e:
        logger.error(f"Error fetching messages for thread {thread_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch messages: {str(e)}"
        )


@router.get("/agent-runs/{agent_run_id}")
async def get_agent_run_deprecated(
    agent_run_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """
    [DEPRECATED] Get an agent run by ID.

    This endpoint is deprecated and may be removed in future versions.
    """
    logger.warning(f"[DEPRECATED] Fetching agent run: {agent_run_id}")
    client = await db.client
    try:
        # 使用正确的访问检查函数
        agent_run_data = await get_agent_run_with_access_check(
            client, agent_run_id, user_id
        )
        return agent_run_data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching agent run {agent_run_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch agent run: {str(e)}"
        )


@router.post("/threads/{thread_id}/messages/add")
async def add_message_to_thread(
    thread_id: str,
    message: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """Add a message to a thread"""
    logger.info(f"Adding message to thread: {thread_id}")
    client = await db.client
    await verify_thread_access(client, thread_id, user_id)
    try:
        # 使用ADK events表记录用户消息
        message_id = str(uuid.uuid4())
        event_id = await _log_adk_user_message_event(
            client, user_id, message, thread_id, message_id
        )

        # 返回消息格式（模拟原messages表结构）
        return {
            "message_id": message_id,
            "thread_id": thread_id,
            "type": "user",
            "content": {"role": "user", "content": message},
            "event_id": event_id,
        }
    except Exception as e:
        logger.error(f"Error adding message to thread {thread_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Failed to add message: {str(e)}")


@router.post("/threads/{thread_id}/messages")
async def create_message(
    thread_id: str,
    message_data: MessageCreateRequest,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """Create a new message in a thread."""
    logger.info(f"Creating message in thread: {thread_id}")
    client = await db.client

    try:
        await verify_thread_access(client, thread_id, user_id)

        message_payload = {
            "role": "user" if message_data.type == "user" else "assistant",
            "content": message_data.content,
        }

        insert_data = {
            "message_id": str(uuid.uuid4()),
            "thread_id": thread_id,
            "type": message_data.type,
            "is_llm_message": message_data.is_llm_message,
            "content": message_payload,  # Store as JSONB object, not JSON string
            "created_at": datetime.now(),
        }

        # 使用ADK events表记录消息
        if message_data.type == "user":
            event_id = await _log_adk_user_message_event(
                client,
                user_id,
                message_payload.get("content", ""),
                thread_id,
                insert_data["message_id"],
                media_refs=message_data.media_refs,
            )
        else:
            event_id = await _log_adk_agent_response_event(
                client,
                user_id,
                message_payload.get("content", ""),
                thread_id,
                "unknown",
            )

        # 构建返回数据（模拟原messages表结构）
        created_message = {
            "message_id": insert_data["message_id"],
            "thread_id": thread_id,
            "type": message_data.type,
            "is_llm_message": message_data.is_llm_message,
            "content": message_payload,
            "created_at": insert_data["created_at"].isoformat(),
            "event_id": event_id,
        }

        logger.info(f"Created message: {created_message['message_id']}")
        return created_message

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error creating message in thread {thread_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to create message: {str(e)}"
        )


@router.delete("/threads/{thread_id}/messages/{message_id}")
async def delete_message(
    thread_id: str,
    message_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """Delete a message from a thread."""
    logger.info(f"Deleting message from thread: {thread_id}")
    client = await db.client
    await verify_thread_access(client, thread_id, user_id)
    try:
        # Don't allow users to delete the "status" messages
        # 从ADK events表删除消息（通过message_id在content中查找）
        await client.schema("public").table("events").eq(
            "session_id", thread_id
        ).filter("content", "cs", f'{{"message_id":"{message_id}"}}').delete()
        return {"message": "Message deleted successfully"}
    except Exception as e:
        logger.error(
            f"Error deleting message {message_id} from thread {thread_id}: {str(e)}"
        )
        raise HTTPException(
            status_code=500, detail=f"Failed to delete message: {str(e)}"
        )


@router.delete("/threads/{thread_id}")
async def delete_thread_endpoint(
    thread_id: str,
    delete_sandbox_flag: bool = Query(
        default=False,
        alias="delete_sandbox",
        description="Whether to also delete the associated sandbox",
    ),
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """
    Delete a thread and all its associated data.

    This endpoint deletes:
    1. ADK sessions and events (via cascade)
    2. Agent runs associated with the thread
    3. The thread itself (messages cascade delete automatically)
    4. Optionally the associated sandbox
    """
    logger.info(
        f"Deleting thread: {thread_id}, user_id: {user_id}, delete_sandbox: {delete_sandbox_flag}"
    )
    client = await db.client

    # Verify user has access to this thread
    await verify_thread_access(client, thread_id, user_id)

    try:
        # Get thread info (for sandbox deletion if needed)
        thread_result = (
            await client.table("threads")
            .select("project_id")
            .eq("thread_id", thread_id)
            .execute()
        )

        if not thread_result.data:
            raise HTTPException(status_code=404, detail="Thread not found")

        project_id = thread_result.data[0].get("project_id")
        sandbox_id = None

        # Get sandbox info if we need to delete it
        if delete_sandbox_flag and project_id:
            project_result = (
                await client.table("projects")
                .select("sandbox")
                .eq("project_id", project_id)
                .execute()
            )
            if project_result.data:
                sandbox_data = project_result.data[0].get("sandbox")
                if isinstance(sandbox_data, str):
                    try:
                        sandbox_data = json.loads(sandbox_data)
                    except (json.JSONDecodeError, TypeError):
                        sandbox_data = {}
                if sandbox_data:
                    sandbox_id = sandbox_data.get("id")

        # Step 1: Delete ADK sessions (this will cascade delete events)
        try:
            await client.table("sessions").eq("id", thread_id).delete()
            logger.info(f"Deleted ADK session for thread: {thread_id}")
        except Exception as e:
            logger.warning(
                f"Error deleting ADK session for thread {thread_id}: {str(e)}"
            )
            # Continue even if this fails - the session might not exist

        # Step 2: Delete agent_runs (no cascade delete configured)
        try:
            await client.table("agent_runs").eq("thread_id", thread_id).delete()
            logger.info(f"Deleted agent_runs for thread: {thread_id}")
        except Exception as e:
            logger.warning(
                f"Error deleting agent_runs for thread {thread_id}: {str(e)}"
            )

        # Step 3: Delete the thread itself (messages will cascade delete)
        delete_result = (
            await client.table("threads").eq("thread_id", thread_id).delete()
        )

        if not delete_result.data:
            logger.warning(
                f"Thread {thread_id} may have already been deleted or deletion failed silently"
            )
        else:
            logger.info(f"Successfully deleted thread: {thread_id}")

        cleanup_job_id = None
        cleanup_status = "skipped"

        # Step 4: Optionally delete the sandbox (async queue; do not block API request)
        if delete_sandbox_flag and sandbox_id:
            try:
                cleanup_message = delete_sandbox_background.send(
                    sandbox_id=sandbox_id,
                    thread_id=thread_id,
                    project_id=project_id,
                )
                cleanup_job_id = cleanup_message.message_id
                cleanup_status = "queued"
                logger.info(
                    "Queued sandbox cleanup (sandbox_id=%s, thread_id=%s, message_id=%s)",
                    sandbox_id,
                    thread_id,
                    cleanup_job_id,
                )
            except Exception as e:
                cleanup_status = "enqueue_failed"
                logger.warning(
                    "Failed to queue sandbox cleanup for sandbox %s: %s",
                    sandbox_id,
                    str(e),
                )
                # Don't fail thread deletion if cleanup queueing fails.

        return {
            "message": "Thread deleted successfully",
            "thread_id": thread_id,
            "sandbox_deleted": False,
            "sandbox_cleanup_requested": delete_sandbox_flag and sandbox_id is not None,
            "sandbox_cleanup_status": cleanup_status,
            "sandbox_cleanup_job_id": cleanup_job_id,
            "deletion_mode": "async_cleanup",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting thread {thread_id}: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to delete thread: {str(e)}"
        )


@router.put("/agents/{agent_id}/custom-mcp-tools")
async def update_agent_custom_mcps(
    agent_id: str, request: dict, user_id: str = Depends(get_current_user_id_from_jwt)
):
    logger.info(f"Updating agent {agent_id} custom MCPs for user {user_id}")

    try:
        client = await db.client
        agent_result = (
            await client.table("agents")
            .select("current_version_id")
            .eq("agent_id", agent_id)
            .eq("account_id", user_id)
            .execute()
        )
        if not agent_result.data:
            raise HTTPException(status_code=404, detail="Agent not found")

        agent = agent_result.data[0]

        agent_config = {}
        if agent.get("current_version_id"):
            version_result = (
                await client.table("agent_versions")
                .select("config")
                .eq("version_id", agent["current_version_id"])
                .maybe_single()
                .execute()
            )
            if version_result.data and version_result.data.get("config"):
                agent_config = version_result.data["config"]

        new_custom_mcps = request.get("custom_mcps", [])
        if not new_custom_mcps:
            raise HTTPException(status_code=400, detail="custom_mcps array is required")

        tools = agent_config.get("tools", {})
        existing_custom_mcps = tools.get("custom_mcp", [])

        updated = False
        for new_mcp in new_custom_mcps:
            mcp_type = new_mcp.get("type", "")

            if mcp_type == "composio":
                profile_id = new_mcp.get("config", {}).get("profile_id")
                if not profile_id:
                    continue

                for i, existing_mcp in enumerate(existing_custom_mcps):
                    if (
                        existing_mcp.get("type") == "composio"
                        and existing_mcp.get("config", {}).get("profile_id")
                        == profile_id
                    ):
                        existing_custom_mcps[i] = new_mcp
                        updated = True
                        break

                if not updated:
                    existing_custom_mcps.append(new_mcp)
                    updated = True
            else:
                mcp_url = new_mcp.get("config", {}).get("url")
                mcp_name = new_mcp.get("name", "")

                for i, existing_mcp in enumerate(existing_custom_mcps):
                    if existing_mcp.get("config", {}).get("url") == mcp_url or (
                        mcp_name and existing_mcp.get("name") == mcp_name
                    ):
                        existing_custom_mcps[i] = new_mcp
                        updated = True
                        break

                if not updated:
                    existing_custom_mcps.append(new_mcp)
                    updated = True

        tools["custom_mcp"] = existing_custom_mcps
        agent_config["tools"] = tools

        from agent.versioning.version_service import get_version_service
        import datetime

        try:
            version_service = await get_version_service()
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            change_description = f"MCP tools update {timestamp}"

            new_version = await version_service.create_version(
                agent_id=agent_id,
                user_id=user_id,
                system_prompt=agent_config.get("system_prompt", ""),
                configured_mcps=agent_config.get("tools", {}).get("mcp", []),
                custom_mcps=existing_custom_mcps,
                agentpress_tools=agent_config.get("tools", {}).get("agentpress", {}),
                change_description=change_description,
            )
            logger.info(
                f"Created version {new_version.version_id} for agent {agent_id}"
            )

            total_enabled_tools = sum(
                len(mcp.get("enabledTools", [])) for mcp in new_custom_mcps
            )
        except Exception as e:
            logger.error(f"Failed to create version for custom MCP tools update: {e}")
            raise HTTPException(status_code=500, detail="Failed to save changes")

        return {
            "success": True,
            "data": {
                "custom_mcps": existing_custom_mcps,
                "total_enabled_tools": total_enabled_tools,
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating agent custom MCPs: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/tools/export-presentation")
async def export_presentation(
    request: Dict[str, Any] = Body(...),
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    try:
        presentation_name = request.get("presentation_name")
        export_format = request.get("format", "pptx")
        project_id = request.get("project_id")

        if not presentation_name:
            raise HTTPException(status_code=400, detail="presentation_name is required")

        if not project_id:
            raise HTTPException(status_code=400, detail="project_id is required")

        if db is None:
            db_conn = DBConnection()
            client = await db_conn.client
        else:
            client = await db.client

        project_result = (
            await client.table("projects")
            .select("sandbox")
            .eq("project_id", project_id)
            .execute()
        )
        if not project_result.data:
            raise HTTPException(status_code=404, detail="Project not found")

        sandbox_data = project_result.data[0].get("sandbox", {})
        sandbox_id = sandbox_data.get("id")

        if not sandbox_id:
            raise HTTPException(
                status_code=400, detail="No sandbox found for this project"
            )

        thread_manager = ThreadManager()

        presentation_tool = SandboxPresentationToolV2(
            project_id=project_id, thread_manager=thread_manager
        )

        result = await presentation_tool.export_presentation(
            presentation_name=presentation_name, format=export_format
        )

        if result.success:
            import json
            import urllib.parse

            data = json.loads(result.output)

            export_file = data.get("export_file")
            logger.info(f"Export file from tool: {export_file}")
            logger.info(f"Sandbox ID: {sandbox_id}")

            if export_file:
                from fastapi.responses import Response
                from sandbox.api import get_sandbox_by_id_safely, verify_sandbox_access

                try:
                    file_path = export_file.replace("/workspace/", "").lstrip("/")
                    full_path = f"/workspace/{file_path}"

                    sandbox = await get_sandbox_by_id_safely(client, sandbox_id)
                    file_content = sandbox.files.read(full_path, format="bytes")

                    return {
                        "success": True,
                        "message": data.get("message"),
                        "file_content": base64.b64encode(file_content).decode("utf-8"),
                        "filename": export_file.split("/")[-1],
                        "export_file": data.get("export_file"),
                        "format": data.get("format"),
                        "file_size": data.get("file_size"),
                    }
                except Exception as e:
                    logger.error(f"Failed to read exported file: {str(e)}")
                    return {
                        "success": False,
                        "error": f"Failed to read exported file: {str(e)}",
                    }
            else:
                return {
                    "success": True,
                    "message": data.get("message"),
                    "download_url": data.get("download_url"),
                    "export_file": data.get("export_file"),
                    "format": data.get("format"),
                    "file_size": data.get("file_size"),
                }
        else:
            raise HTTPException(
                status_code=400, detail=result.output or "Export failed"
            )

    except Exception as e:
        logger.error(f"Export presentation error: {str(e)}")
        raise HTTPException(
            status_code=500, detail=f"Failed to export presentation: {str(e)}"
        )


@router.post("/agents/profile-image/upload")
async def upload_agent_profile_image(
    file: UploadFile = File(...), user_id: str = Depends(get_current_user_id_from_jwt)
):
    try:
        content_type = file.content_type or "image/png"
        image_bytes = await file.read()
        from utils.s3_upload_utils import upload_image_bytes

        public_url = await upload_image_bytes(
            image_bytes=image_bytes,
            content_type=content_type,
            bucket_name="agent-profile-images",
        )
        return {"url": public_url}
    except Exception as e:
        logger.error(f"Failed to upload agent profile image for user {user_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to upload profile image")


async def _create_adk_session_if_not_exists(
    client, user_id: str, session_id: str, app_name: str = "fufanmanus"
):
    """如果ADK session不存在则创建"""
    try:
        # 检查session是否已存在
        async with client.pool.acquire() as conn:
            existing = await conn.fetchrow(
                """
                SELECT id FROM sessions 
                WHERE app_name = $1 AND user_id = $2 AND id = $3
                """,
                app_name,
                user_id,
                session_id,
            )

            if not existing:
                # 不存在则创建
                await conn.execute(
                    """
                    INSERT INTO sessions (
                        app_name, user_id, id, state, create_time, update_time
                    )
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    app_name,
                    user_id,
                    session_id,
                    "{}",
                    datetime.now(),
                    datetime.now(),
                )
                logger.info(f"Created ADK session: {session_id}")
            else:
                logger.debug(f"ADK session already exists: {session_id}")

    except Exception as e:
        logger.error(f"Create ADK session failed: {e}")
        raise


async def _log_adk_user_message_event(
    client,
    user_id: str,
    message_content: str,
    session_id: str,
    message_id: str,
    app_name: str = "fufanmanus",
    media_refs: Optional[List[Dict[str, Any]]] = None,
):
    """记录用户消息事件到ADK events表"""
    try:
        import uuid
        import pickle
        from datetime import datetime

        event_id = str(uuid.uuid4())
        invocation_id = str(uuid.uuid4())

        parts = build_user_event_parts(message_content, media_refs=media_refs)

        # 使用兼容 ADK 的 events 结构（content.parts）
        content = {
            "role": "user",
            "parts": parts,
        }

        # actions needs to be a pickled ADK EventActions instance (ADK expects `.model_dump()` on load).
        from google.adk.events.event_actions import EventActions  # type: ignore

        actions_bytes = pickle.dumps(EventActions())

        # 插入到ADK events表
        async with client.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (
                    id, app_name, user_id, session_id, invocation_id, 
                    author, timestamp, content, actions
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                event_id,
                app_name,
                user_id,
                session_id,
                invocation_id,
                "user",
                datetime.now(),
                json.dumps(content),
                actions_bytes,
            )

        logger.info(f"User message event recorded successfully: {event_id}")
        return event_id

    except Exception as e:
        logger.error(f"Record user message event failed: {e}")
        raise


async def _log_adk_agent_response_event(
    client,
    user_id: str,
    response_content: str,
    session_id: str,
    model_name: str,
    app_name: str = "fufanmanus",
):
    """记录AI代理回复事件到ADK events表"""
    try:
        import uuid

        event_id = str(uuid.uuid4())
        invocation_id = str(uuid.uuid4())

        # 构建回复内容
        content = {
            "role": "assistant",
            "content": response_content,
            "model": model_name,
        }

        # ADK expects actions to be a pickled EventActions instance.
        import pickle
        from google.adk.events.event_actions import EventActions  # type: ignore

        actions_bytes = pickle.dumps(EventActions())

        # 插入到ADK events表
        async with client.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO events (
                    id, app_name, user_id, session_id, invocation_id, 
                    author, timestamp, content, actions, turn_complete
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                """,
                event_id,
                app_name,
                user_id,
                session_id,
                invocation_id,
                "assistant",
                datetime.now(),
                json.dumps(content),
                actions_bytes,
                True,  # turn_complete=True
            )

        logger.info(f"记录AI回复事件成功: {event_id}")
        return event_id

    except Exception as e:
        logger.error(f"记录AI回复事件失败: {e}")
        raise


def _format_messages_from_table(messages):
    """格式化messages表数据为前端期望格式，支持assistant消息动态拆分"""
    formatted_messages = []

    raw_message_stats = {}
    for msg in messages:
        msg_type = msg.get("type", "unknown")
        raw_message_stats[msg_type] = raw_message_stats.get(msg_type, 0) + 1
    logger.info(f"raw message type stats: {raw_message_stats}")

    # 特别检查原始assistant消息
    raw_assistant_messages = [msg for msg in messages if msg.get("type") == "assistant"]
    if raw_assistant_messages:
        for assistant_msg in raw_assistant_messages:
            raw_msg_id = assistant_msg.get("message_id")
            logger.info(
                "found raw assistant message: "
                f"ID={raw_msg_id} (type: {type(raw_msg_id)}), "
                f"metadata_event_id={_safe_json_object(assistant_msg.get('metadata')).get('event_id')}, "
                f"metadata_source={_safe_json_object(assistant_msg.get('metadata')).get('source')}"
            )
    else:
        logger.warning("no assistant message found")

    for msg in messages:
        try:
            # 处理content字段 - 解析为对象以便后续判断
            content = msg.get("content", {})
            content_obj = content
            if isinstance(content, str):
                try:
                    import json

                    content_obj = json.loads(content)
                    content_str = content
                except:
                    content_str = str(content) if content else "{}"
                    content_obj = {}
            elif isinstance(content, dict):
                import json

                content_str = json.dumps(content, ensure_ascii=False)
                content_obj = content
            else:
                content_str = str(content) if content else "{}"
                content_obj = {}

            # 🔧 处理metadata字段 - 解析为对象以便后续判断
            metadata = msg.get("metadata", {})
            metadata_obj = metadata
            if isinstance(metadata, str):
                try:
                    import json

                    metadata_obj = json.loads(metadata)
                    metadata_str = metadata
                except:
                    metadata_str = str(metadata) if metadata else "{}"
                    metadata_obj = {}
            elif isinstance(metadata, dict):
                import json

                metadata_str = json.dumps(metadata, ensure_ascii=False)
                metadata_obj = metadata
            else:
                metadata_str = str(metadata) if metadata else "{}"
                metadata_obj = {}

            # 🔧 检查是否需要拆分assistant消息
            if (
                msg.get("type") == "assistant"
                and metadata_obj.get("split_for_frontend") == True
                and metadata_obj.get("tool_call_mapping")
            ):

                logger.info(
                    f"🔧 检测到需要拆分的assistant消息: {msg.get('message_id')}"
                )
                tool_call_mapping = metadata_obj.get("tool_call_mapping", [])
                assistant_text = content_obj.get("content", "")
                tool_calls = content_obj.get("tool_calls", [])

                # 为每个tool_call创建单独的assistant消息
                for mapping in tool_call_mapping:
                    index = mapping.get("index", 0)
                    tool_call_id = mapping.get("tool_call_id", "")
                    include_text = mapping.get("include_text", False)

                    # 找到对应的tool_call对象
                    matching_tool_call = None
                    for tc in tool_calls:
                        if tc.get("id") == tool_call_id:
                            matching_tool_call = tc
                            break

                    if matching_tool_call:
                        # 🔧 生成确定性UUID（与agent/run.py保持一致）
                        import hashlib

                        seed_data = f"assistant_split_{tool_call_id}_{msg.get('thread_id')}_{index}_v1"
                        hash_object = hashlib.md5(seed_data.encode())
                        hex_dig = hash_object.hexdigest()
                        deterministic_uuid = f"{hex_dig[:8]}-{hex_dig[8:12]}-{hex_dig[12:16]}-{hex_dig[16:20]}-{hex_dig[20:]}"

                        # 构建拆分后的消息内容
                        split_content = {
                            "role": "assistant",
                            "content": assistant_text if include_text else "",
                            "tool_calls": [matching_tool_call],
                        }

                        # 构建拆分后的元数据
                        split_metadata = metadata_obj.copy()
                        split_metadata["tool_index"] = index
                        split_metadata["original_message_id"] = (
                            str(msg.get("message_id"))
                            if msg.get("message_id")
                            else None
                        )

                        # 创建拆分后的消息 - 确保所有UUID字段都是字符串
                        split_message = {
                            "message_id": deterministic_uuid,
                            "thread_id": (
                                str(msg.get("thread_id"))
                                if msg.get("thread_id")
                                else None
                            ),
                            "type": "assistant",
                            "role": "assistant",
                            "is_llm_message": msg.get("is_llm_message", False),
                            "content": json.dumps(split_content, ensure_ascii=False),
                            "metadata": json.dumps(split_metadata, ensure_ascii=False),
                            "created_at": msg.get("created_at"),
                            "updated_at": msg.get("updated_at"),
                            "agent_id": (
                                str(msg.get("agent_id"))
                                if msg.get("agent_id")
                                else None
                            ),
                            "agent_version_id": (
                                str(msg.get("agent_version_id"))
                                if msg.get("agent_version_id")
                                else None
                            ),
                        }

                        formatted_messages.append(split_message)
                        logger.info(
                            f"split assistant message: {deterministic_uuid} (tool: {matching_tool_call.get('function', {}).get('name', 'unknown')})"
                        )
                        logger.debug(
                            f"split message field type check: message_id={type(deterministic_uuid)}, thread_id={type(split_message['thread_id'])}"
                        )

            else:
                # 🔧 普通消息处理逻辑 - 确保所有UUID字段都是字符串
                formatted_message = {
                    "message_id": (
                        str(msg.get("message_id")) if msg.get("message_id") else None
                    ),
                    "thread_id": (
                        str(msg.get("thread_id")) if msg.get("thread_id") else None
                    ),
                    "type": msg.get("type"),  # assistant, user, tool, status等
                    "role": msg.get("role"),  # assistant, user, system等
                    "is_llm_message": msg.get("is_llm_message", False),
                    "content": content_str,  # JSON字符串格式
                    "metadata": metadata_str,  # JSON字符串格式
                    "created_at": msg.get("created_at"),
                    "updated_at": msg.get("updated_at"),
                    "agent_id": (
                        str(msg.get("agent_id")) if msg.get("agent_id") else None
                    ),
                    "agent_version_id": (
                        str(msg.get("agent_version_id"))
                        if msg.get("agent_version_id")
                        else None
                    ),
                }

                formatted_messages.append(formatted_message)

        except Exception as e:
            logger.warning(
                f"skip format error message {msg.get('message_id', 'unknown')}: {e}"
            )
            continue

            # 🔧 更新tool消息的assistant_message_id关联
    assistant_messages = [
        msg for msg in formatted_messages if msg.get("type") == "assistant"
    ]
    tool_messages = [msg for msg in formatted_messages if msg.get("type") == "tool"]

    # 创建tool_call_id到assistant_message_id的映射
    tool_call_to_assistant = {}
    for assistant_msg in assistant_messages:
        try:
            content = assistant_msg.get("content", {})
            if isinstance(content, str):
                content = json.loads(content)

            tool_calls = content.get("tool_calls", [])
            for tool_call in tool_calls:
                tool_call_id = tool_call.get("id")
                if tool_call_id:
                    tool_call_to_assistant[tool_call_id] = assistant_msg.get(
                        "message_id"
                    )
        except Exception as e:
            logger.warning(f"parse assistant message content failed: {e}")

    # 更新tool消息的assistant_message_id
    updated_tool_count = 0
    for tool_msg in tool_messages:
        try:
            metadata = tool_msg.get("metadata", {})
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            tool_call_id = metadata.get("tool_call_id")
            if tool_call_id and tool_call_id in tool_call_to_assistant:
                correct_assistant_id = tool_call_to_assistant[tool_call_id]
                metadata["assistant_message_id"] = correct_assistant_id
                tool_msg["metadata"] = json.dumps(metadata, ensure_ascii=False)
                updated_tool_count += 1
                logger.info(
                    f"update tool message {tool_msg.get('message_id')} -> assistant {correct_assistant_id}"
                )
        except Exception as e:
            logger.warning(
                f"update tool message assistant failed {tool_msg.get('message_id')}: {e}"
            )

    # 🔍 最终统计
    logger.info(f"final assistant message count: {len(assistant_messages)}")
    logger.info(f"tool message count: {len(tool_messages)}")
    logger.info(f"updated {updated_tool_count} tool message assistant")

    return formatted_messages


def _convert_user_events_to_messages(events):
    """将用户events转换为前端期望的消息格式"""
    user_messages = []

    for event in events:
        try:
            # 🔧 解析content字段
            content = event.get("content")
            if isinstance(content, str):
                import json

                content = json.loads(content)

            # 🔧 提取用户文本内容
            user_text = ""
            if isinstance(content, dict) and "parts" in content:
                text_parts = []
                for part in content["parts"]:
                    if isinstance(part, dict) and "text" in part:
                        text_parts.append(part["text"].strip())
                user_text = " ".join(text_parts).strip()
            elif isinstance(content, dict) and "content" in content:
                user_text = content["content"]
            else:
                user_text = str(content)

            # 🔧 构建前端期望的用户消息格式
            import json

            user_content = {"role": "user", "content": user_text}

            formatted_message = {
                "message_id": str(event.get("id")) if event.get("id") else None,
                "thread_id": (
                    str(event.get("session_id")) if event.get("session_id") else None
                ),
                "type": "user",
                "role": "user",
                "is_llm_message": False,
                "content": json.dumps(user_content, ensure_ascii=False),  # JSON字符串
                "metadata": "{}",  # 空metadata
                "created_at": event.get("timestamp"),
                "updated_at": event.get("timestamp"),  # 使用timestamp作为updated_at
                "agent_id": None,
                "agent_version_id": None,
            }

            user_messages.append(formatted_message)
            logger.debug(
                f"转换用户消息: {event.get('id')} - {user_text[:50]}{'...' if len(user_text) > 50 else ''}"
            )

        except Exception as e:
            logger.warning(f"跳过格式错误的用户事件 {event.get('id', 'unknown')}: {e}")
            continue

    logger.info(f"🔄 转换了 {len(user_messages)} 条用户消息")
    return user_messages
