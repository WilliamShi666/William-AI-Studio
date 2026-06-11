"""
ClaudeSDKRunner — Claude Agent SDK backend for WilliamManus.

Implements the same runner interface as AgentScopeRunner and ShadowCloneRunner:
  async def run(user_message, thread_run_id, ...) -> AsyncGenerator[dict, None]

Communicates with a sandbox-resident Claude Agent Service that wraps
claude_agent_sdk to drive Claude Code inside a PPIO sandbox.
"""

import json as _json
import hashlib
import logging
import os
import posixpath
import re
from pathlib import Path
from collections.abc import Sequence
from typing import Any, AsyncGenerator, Dict, List, Optional

from .claude_sdk_bridge import FakeClaudeBridge, LocalClaudeBridge, SandboxClaudeBridge
from .claude_sdk_sse import ClaudeSDKSSEAdapter

logger = logging.getLogger(__name__)

_SAFE_WORKSPACE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")

_WORKSPACE_ROOT = "/workspace"
_HISTORY_MAX_MESSAGES = 24
_HISTORY_MAX_CHARS = 16_000
_HISTORY_QUERY_EXTRA_ROWS = 8


def _safe_workspace_segment(value: str, *, fallback: str) -> str:
    candidate = _SAFE_WORKSPACE_SEGMENT.sub("-", str(value or "")).strip(".-_")
    return candidate or fallback


def _build_local_workspace_dir(*, agent_run_id: str) -> str:
    root = os.getenv("CLAUDE_LOCAL_WORKSPACE_ROOT", "/tmp/william-claude-local").strip()
    if not root:
        root = "/tmp/william-claude-local"
    safe_run_id = _safe_workspace_segment(agent_run_id, fallback="run")
    return str(Path(root) / "runs" / safe_run_id)


class ClaudeSDKRunner:
    """Runner that delegates to Claude Agent SDK running inside a PPIO sandbox."""

    def __init__(
        self,
        *,
        thread_id: str,
        project_id: str,
        model_key: str,
        db_client,
        trace=None,
        system_prompt: Optional[str] = None,
    ) -> None:
        self.thread_id = thread_id
        self.project_id = project_id
        self.model_key = model_key
        self.db_client = db_client
        self._trace = trace
        self.system_prompt = system_prompt or ""
        self._bridge = None  # set during run()

    async def run(
        self,
        user_message: str,
        thread_run_id: str,
        user_media_refs: Optional[List[Dict[str, Any]]] = None,
        resume_strategy: str = "auto",
        resume_window_minutes: int = 1440,
    ) -> AsyncGenerator[dict, None]:
        """Run the Claude agent and yield SSE-formatted streaming output."""
        collected_messages: List[Dict[str, Any]] = []
        artifacts_persisted = False

        async def _persist_artifacts() -> None:
            nonlocal artifacts_persisted
            if artifacts_persisted or self._bridge is None:
                return
            try:
                await _persist_run_messages(
                    messages=collected_messages,
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    db_client=self.db_client,
                )
            except Exception as persist_exc:
                logger.warning(
                    "[ClaudeSDKRunner] Failed to persist run messages: %s",
                    persist_exc,
                )
            try:
                await _persist_workspace_files(
                    bridge=self._bridge,
                    agent_run_id=thread_run_id,
                    project_id=self.project_id,
                    thread_id=self.thread_id,
                    workspace_dir_override=getattr(self, "_workspace_dir", None),
                )
            except Exception as persist_exc:
                logger.warning(
                    "[ClaudeSDKRunner] Failed to persist workspace files: %s",
                    persist_exc,
                )
            artifacts_persisted = True

        try:
            bridge = getattr(self, "_test_bridge", None)
            if bridge is None:
                bridge = await self._get_bridge(thread_run_id=thread_run_id)
            self._bridge = bridge
            adapter = ClaudeSDKSSEAdapter(
                thread_id=self.thread_id,
                thread_run_id=thread_run_id,
            )

            # Snapshot workspace_dir before the run so persistence survives
            # any bridge state changes during close().
            self._workspace_dir = getattr(bridge, "workspace_dir", None)

            prompt = await _build_prompt_with_history(
                db_client=self.db_client,
                thread_id=self.thread_id,
                user_message=user_message,
            )

            await _rehydrate_thread_workspace(
                bridge=bridge,
                project_id=self.project_id,
                thread_id=self.thread_id,
                db_client=self.db_client,
            )

            await bridge.start(
                prompt=prompt,
                thread_id=self.thread_id,
                system_prompt=self.system_prompt,
            )

            async for msg in bridge.read_stream():
                sse = adapter.convert(msg)
                if sse is not None:
                    collected_messages.append(sse)
                    if sse.get("type") == "status" and sse.get("status") in {
                        "completed",
                        "failed",
                        "stopped",
                        "error",
                    }:
                        await _persist_artifacts()
                    yield sse

        except Exception as exc:
            logger.error("[ClaudeSDKRunner] Run failed: %s", exc)
            status_msg = {
                "type": "status",
                "status": "failed",
                "message": str(exc),
            }
            collected_messages.append(status_msg)
            await _persist_artifacts()
            yield status_msg
        finally:
            # Persist messages and workspace files so they survive
            # GeneratorExit thrown by aclose() after the last yield.
            if self._bridge is not None:
                if not artifacts_persisted:
                    await _persist_artifacts()

    async def close(self) -> None:
        """Clean up sandbox bridge resources.

        Workspace files are persisted during run() BEFORE close()
        to ensure they survive cleanup.
        """
        if self._bridge is not None:
            try:
                await self._bridge.cleanup()
            except Exception as exc:
                logger.warning("[ClaudeSDKRunner] cleanup error: %s", exc)
            finally:
                self._bridge = None

    async def _get_bridge(self, thread_run_id: Optional[str] = None):
        """Return the sandbox bridge.

        In production, creates a SandboxClaudeBridge. With
        CLAUDE_LOCAL_MODE=1, uses LocalClaudeBridge (no sandbox needed).
        """
        deepseek_api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
        if not deepseek_api_key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set. Set DEEPSEEK_API_KEY to use Claude SDK backend."
            )

        if os.getenv("CLAUDE_LOCAL_MODE", "").strip() == "1":
            logger.info("[ClaudeSDKRunner] Using LocalClaudeBridge (no sandbox)")
            return LocalClaudeBridge(
                deepseek_api_key=deepseek_api_key,
                model="deepseek-v4-pro",
                cwd=_build_local_workspace_dir(
                    agent_run_id=thread_run_id or self.thread_id or "run",
                ),
            )

        template_name = os.getenv("CLAUDE_SANDBOX_TEMPLATE", "claude-agent-v1").strip() or "claude-agent-v1"
        template_id = os.getenv("CLAUDE_SANDBOX_TEMPLATE_ID", "").strip()

        return SandboxClaudeBridge(
            deepseek_api_key=deepseek_api_key,
            template_name=template_name,
            template_id=template_id,
            model="deepseek-v4-pro[1m]",
        )

    def _set_bridge_for_testing(self, bridge) -> None:
        """Inject a bridge for testing. Replaces _get_bridge behavior."""
        self._test_bridge = bridge


async def _build_prompt_with_history(
    *,
    db_client,
    thread_id: str,
    user_message: str,
    max_messages: int = _HISTORY_MAX_MESSAGES,
    max_chars: int = _HISTORY_MAX_CHARS,
) -> str:
    """Prepend a bounded durable transcript to the current Claude SDK prompt."""
    history = await _load_text_history(
        db_client=db_client,
        thread_id=thread_id,
        max_messages=max_messages,
        max_chars=max_chars,
        current_user_message=user_message,
    )
    if not history:
        return user_message

    lines = "\n".join(f"{item['role']}: {item['text']}" for item in history)
    return (
        "<conversation_history>\n"
        "Recent prior conversation turns, oldest first. Use this as context; "
        "do not repeat it unless relevant.\n"
        f"{lines}\n"
        "</conversation_history>\n\n"
        "<current_user_message>\n"
        f"{user_message}\n"
        "</current_user_message>"
    )


async def _load_text_history(
    *,
    db_client,
    thread_id: str,
    max_messages: int,
    max_chars: int,
    current_user_message: str,
) -> List[Dict[str, str]]:
    try:
        messages_result = await _execute_history_query(
            db_client.table("messages")
            .select("*")
            .eq("thread_id", thread_id)
            .order("created_at", desc=True)
            .limit(max_messages + _HISTORY_QUERY_EXTRA_ROWS),
        )
        events_result = await _execute_history_query(
            db_client.schema("public")
            .table("events")
            .select("*")
            .eq("session_id", thread_id)
            .eq("author", "user")
            .order("timestamp", desc=True)
            .limit(max_messages + _HISTORY_QUERY_EXTRA_ROWS),
        )
    except Exception as exc:
        logger.warning("[ClaudeSDKRunner] Failed to load prompt history: %s", exc)
        return []

    rows = _merge_history_rows(
        message_rows=messages_result.data or [],
        user_event_rows=events_result.data or [],
    )

    entries: List[Dict[str, str]] = []
    for row in rows:
        role = _coerce_history_role(row)
        if role not in {"user", "assistant"}:
            continue
        text = _extract_history_text(row.get("content"))
        if not text:
            continue
        entries.append({"role": role, "text": text})

    while entries and entries[-1]["role"] == "user" and _same_message_text(
        entries[-1]["text"],
        current_user_message,
    ):
        entries.pop()

    if len(entries) > max_messages:
        entries = entries[-max_messages:]

    total = 0
    trimmed_reversed: List[Dict[str, str]] = []
    for entry in reversed(entries):
        entry_len = len(entry["role"]) + len(entry["text"]) + 3
        if trimmed_reversed and total + entry_len > max_chars:
            break
        if entry_len > max_chars:
            text_limit = max(0, max_chars - len(entry["role"]) - 20)
            entry = {
                "role": entry["role"],
                "text": entry["text"][:text_limit] + "...<history truncated>",
            }
            entry_len = len(entry["role"]) + len(entry["text"]) + 3
        trimmed_reversed.append(entry)
        total += entry_len

    return list(reversed(trimmed_reversed))


def _coerce_history_role(row: Dict[str, Any]) -> str:
    role = str(row.get("role") or "").strip().lower()
    if role in {"user", "assistant"}:
        return role
    row_type = str(row.get("type") or "").strip().lower()
    return row_type if row_type in {"user", "assistant"} else ""


async def _execute_history_query(query: Any):
    execute = query.execute()
    if hasattr(execute, "__await__"):
        return await execute
    return execute


def _merge_history_rows(
    *,
    message_rows: List[Dict[str, Any]],
    user_event_rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized_message_rows = list(message_rows)
    normalized_user_rows = _normalize_user_event_history_rows(user_event_rows)
    if normalized_user_rows:
        normalized_message_rows = [
            row for row in normalized_message_rows if _coerce_history_role(row) != "user"
        ]
    rows = [*normalized_message_rows, *normalized_user_rows]
    rows.sort(key=lambda row: str(row.get("created_at") or ""))
    return rows


def _normalize_user_event_history_rows(
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for row in rows:
        normalized.append(
            {
                "type": "user",
                "role": "user",
                "content": {
                    "role": "user",
                    "content": _extract_event_text(row.get("content")),
                },
                "created_at": row.get("timestamp") or row.get("created_at") or "",
            }
        )
    return normalized


def _extract_event_text(content: Any) -> str:
    if isinstance(content, str):
        try:
            content = _json.loads(content)
        except Exception:
            return " ".join(content.split())
    if isinstance(content, dict):
        for key in ("content", "text"):
            value = content.get(key)
            if isinstance(value, str):
                return " ".join(value.split())
        parts = content.get("parts")
        if isinstance(parts, list):
            return " ".join(
                str(part.get("text") or part.get("content") or "")
                for part in parts
                if isinstance(part, dict)
            ).strip()
    if isinstance(content, list):
        return " ".join(_extract_event_text(item) for item in content).strip()
    return ""


def _extract_history_text(content: Any) -> str:
    content_obj = _decode_json_object(content, {})
    value = content_obj.get("content")
    if isinstance(value, str):
        return " ".join(value.split())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        parts: List[str] = []
        for item in value:
            if isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(" ".join(parts).split())
    return ""


def _same_message_text(a: str, b: str) -> bool:
    return " ".join(str(a or "").split()) == " ".join(str(b or "").split())


async def _persist_run_messages(
    *,
    messages: List[Dict[str, Any]],
    thread_id: str,
    project_id: str,
    db_client,
) -> None:
    """Persist collected SSE messages to the messages table so they survive page refresh."""
    if not messages:
        return

    aggregate_content, aggregate_metadata = _build_aggregate_assistant_message(messages)
    has_persisted_assistant = False
    has_persisted_aggregate = False
    persisted = 0
    for sse in messages:
        try:
            msg_type = sse.get("type", "")

            # Persist status messages and complete messages (is_last=True, which
            # sets a non-None message_id). Streaming chunks have message_id=None.
            is_status_msg = msg_type in ("status",)
            is_complete_msg = bool(sse.get("message_id"))
            if not is_status_msg and not is_complete_msg:
                continue

            is_llm = bool(sse.get("is_llm_message", False))

            if msg_type == "assistant":
                role = "assistant"
            elif msg_type == "tool":
                role = "tool"
            elif msg_type == "status":
                role = "assistant"
            else:
                continue

            mb_content = sse.get("content", "{}")
            if is_status_msg:
                content = {"status": sse.get("status", ""), "message": sse.get("message", "")}
            elif isinstance(mb_content, str):
                try:
                    content = _json.loads(mb_content)
                except (_json.JSONDecodeError, TypeError):
                    content = {"text": str(mb_content)}
            else:
                content = mb_content

            mb_metadata = sse.get("metadata", "{}")
            if isinstance(mb_metadata, str):
                try:
                    metadata = _json.loads(mb_metadata)
                except (_json.JSONDecodeError, TypeError):
                    metadata = {}
            else:
                metadata = mb_metadata

            if msg_type == "assistant" and not _is_renderable_assistant_content(content):
                if aggregate_content and not has_persisted_aggregate:
                    content = aggregate_content
                    metadata = {**metadata, **aggregate_metadata}
                    has_persisted_assistant = True
                    has_persisted_aggregate = True
                else:
                    continue
            elif msg_type == "assistant":
                has_persisted_assistant = True

            await db_client.table("messages").insert({
                "thread_id": thread_id,
                "project_id": project_id,
                "type": msg_type,
                "role": role,
                "content": _json.dumps(content, ensure_ascii=False),
                "is_llm_message": is_llm,
                "metadata": _json.dumps(metadata, ensure_ascii=False),
            })
            persisted += 1
        except Exception as exc:
            logger.warning("[ClaudeSDKRunner] Failed to persist message: %s", exc)

    if aggregate_content and not has_persisted_assistant:
        try:
            await db_client.table("messages").insert({
                "thread_id": thread_id,
                "project_id": project_id,
                "type": "assistant",
                "role": "assistant",
                "content": _json.dumps(aggregate_content, ensure_ascii=False),
                "is_llm_message": True,
                "metadata": _json.dumps(aggregate_metadata, ensure_ascii=False),
            })
            persisted += 1
        except Exception as exc:
            logger.warning("[ClaudeSDKRunner] Failed to persist aggregate message: %s", exc)

    if persisted:
        logger.info(
            "[ClaudeSDKRunner] Persisted %d/%d messages to messages table",
            persisted, len(messages),
        )


def _build_aggregate_assistant_message(
    messages: List[Dict[str, Any]],
) -> tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """Build a renderable assistant row from transient Claude SDK stream chunks."""
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    seen_tool_call_ids: set[str] = set()
    thread_run_id: Optional[str] = None

    for sse in messages:
        if sse.get("type") != "assistant" or sse.get("message_id"):
            continue

        content = _decode_json_object(sse.get("content"), {})
        metadata = _decode_json_object(sse.get("metadata"), {})
        stream_status = metadata.get("stream_status")

        if not thread_run_id and isinstance(metadata.get("thread_run_id"), str):
            thread_run_id = metadata["thread_run_id"]

        if stream_status == "chunk":
            text = content.get("content")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif stream_status == "reasoning_chunk":
            reasoning = content.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                reasoning_parts.append(reasoning)
        elif stream_status == "tool_call_chunk":
            for tool_call in _coerce_tool_calls(content.get("tool_calls")) + _coerce_tool_calls(
                metadata.get("tool_calls")
            ):
                tool_call_id = str(tool_call.get("id") or "")
                if tool_call_id and tool_call_id in seen_tool_call_ids:
                    continue
                if tool_call_id:
                    seen_tool_call_ids.add(tool_call_id)
                tool_calls.append(tool_call)

    aggregate_content: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if reasoning_parts:
        aggregate_content["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        aggregate_content["tool_calls"] = tool_calls

    if not _is_renderable_assistant_content(aggregate_content):
        return None, {}

    aggregate_metadata: Dict[str, Any] = {
        "stream_status": "complete",
        "source": "claude_sdk_aggregate",
    }
    if thread_run_id:
        aggregate_metadata["thread_run_id"] = thread_run_id
    if tool_calls:
        aggregate_metadata["tool_calls"] = tool_calls

    return aggregate_content, aggregate_metadata


def _decode_json_object(value: Any, fallback: Dict[str, Any]) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = _json.loads(value)
        except (_json.JSONDecodeError, TypeError):
            return fallback
        return decoded if isinstance(decoded, dict) else fallback
    return fallback


def _coerce_tool_calls(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _is_renderable_assistant_content(content: Dict[str, Any]) -> bool:
    text = content.get("content")
    if isinstance(text, str) and text.strip():
        return True
    reasoning = content.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return True
    tool_calls = content.get("tool_calls")
    return isinstance(tool_calls, list) and len(tool_calls) > 0


async def _persist_workspace_files(
    *,
    bridge,
    agent_run_id: str,
    project_id: str,
    thread_id: str,
    workspace_dir_override: Optional[str] = None,
) -> None:
    """Scan the bridge workspace and persist files via workspace_artifacts."""
    from services.workspace_artifacts import (
        is_user_visible_workspace_artifact_path,
        workspace_artifacts,
    )

    workspace_dir = workspace_dir_override or getattr(bridge, "workspace_dir", None)
    if not workspace_dir:
        logger.info("[ClaudeSDKRunner] No workspace dir to persist: %s", workspace_dir)
        return

    persisted = 0
    all_files: list[str] = []
    sandbox_id = getattr(bridge, "sandbox_id", None) or f"claude-local:{agent_run_id}"
    list_workspace_files = getattr(bridge, "list_workspace_files", None)
    read_workspace_file_bytes = getattr(bridge, "read_workspace_file_bytes", None)
    is_remote_workspace = callable(list_workspace_files) and callable(read_workspace_file_bytes)

    async def _persist_entry(workspace_path: str, content: bytes, *, source: str, metadata: dict[str, Any]) -> None:
        nonlocal persisted
        file_size = len(content)
        if file_size > 50 * 1024 * 1024:
            logger.warning(
                "[ClaudeSDKRunner] Skipping large file %s (%d bytes)",
                workspace_path,
                file_size,
            )
            return
        await workspace_artifacts.persist_artifact(
            project_id=project_id,
            path=workspace_path,
            content=content,
            source=source,
            thread_id=thread_id,
            agent_run_id=agent_run_id,
            sandbox_id=sandbox_id,
            metadata={
                "workspace_scope": "agent_run",
                **metadata,
            },
        )
        persisted += 1
        logger.info(
            "[ClaudeSDKRunner] Persisted workspace file: %s (%d bytes)",
            workspace_path,
            file_size,
        )

    if is_remote_workspace:
        try:
            entries = await list_workspace_files(_WORKSPACE_ROOT)
        except Exception as exc:
            logger.warning("[ClaudeSDKRunner] Failed to list sandbox workspace files: %s", exc)
            entries = []
        for entry in entries or []:
            if isinstance(entry, dict):
                workspace_path = str(entry.get("path") or "")
                entry_type = entry.get("type")
                entry_size = entry.get("size")
            else:
                workspace_path = str(getattr(entry, "path", "") or "")
                entry_type = getattr(entry, "type", None)
                entry_size = getattr(entry, "size", None)
            if not workspace_path:
                continue
            all_files.append(workspace_path)
            if entry_type not in (None, "file") and str(entry_type) != "file":
                continue
            if not is_user_visible_workspace_artifact_path(workspace_path):
                continue
            try:
                content = await read_workspace_file_bytes(workspace_path)
                if isinstance(content, str):
                    content = content.encode("utf-8")
                metadata = {
                    "bridge_type": type(bridge).__name__,
                    "sandbox_workspace_dir": workspace_dir,
                }
                if entry_size is not None:
                    metadata["reported_size"] = entry_size
                metadata["sha256"] = hashlib.sha256(content).hexdigest()
                await _persist_entry(
                    workspace_path,
                    content,
                    source="claude_sdk_sandbox",
                    metadata=metadata,
                )
            except Exception as exc:
                logger.warning(
                    "[ClaudeSDKRunner] Failed to persist sandbox workspace file %s: %s",
                    workspace_path,
                    exc,
                )
    elif os.path.isdir(workspace_dir):
        workspace_realpath = os.path.realpath(workspace_dir)
        for dirpath, dirnames, filenames in os.walk(workspace_dir, followlinks=False):
            visible_dirnames = []
            for dirname in dirnames:
                rel_dir = os.path.relpath(os.path.join(dirpath, dirname), workspace_dir)
                workspace_dir_path = posixpath.join(_WORKSPACE_ROOT, rel_dir)
                if is_user_visible_workspace_artifact_path(workspace_dir_path):
                    visible_dirnames.append(dirname)
            dirnames[:] = visible_dirnames

            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                relpath = os.path.relpath(filepath, workspace_dir)
                workspace_path = posixpath.join(_WORKSPACE_ROOT, relpath)
                all_files.append(workspace_path)
                if not is_user_visible_workspace_artifact_path(workspace_path):
                    continue
                try:
                    if os.path.islink(filepath):
                        logger.warning(
                            "[ClaudeSDKRunner] Skipping symlink workspace file: %s",
                            workspace_path,
                        )
                        continue
                    realpath = os.path.realpath(filepath)
                    if realpath != workspace_realpath and not realpath.startswith(f"{workspace_realpath}{os.sep}"):
                        logger.warning(
                            "[ClaudeSDKRunner] Skipping file outside workspace realpath: %s",
                            workspace_path,
                        )
                        continue
                    if not os.path.isfile(filepath):
                        continue
                    with open(filepath, "rb") as f:
                        content = f.read()
                    await _persist_entry(
                        workspace_path,
                        content,
                        source="claude_sdk_local",
                        metadata={"local_workspace_dir": workspace_dir},
                    )
                except Exception as exc:
                    logger.warning(
                        "[ClaudeSDKRunner] Failed to persist workspace file %s: %s",
                        workspace_path,
                        exc,
                    )
    else:
        logger.info(
            "[ClaudeSDKRunner] Bridge does not expose a readable local workspace or sandbox file API; skipping persistence for dir=%s",
            workspace_dir,
        )
        return

    logger.info(
        "[ClaudeSDKRunner] Workspace scan complete: dir=%s total_files=%d visible_persisted=%d run=%s",
        workspace_dir,
        len(all_files),
        persisted,
        agent_run_id,
    )


async def _rehydrate_thread_workspace(
    *,
    bridge,
    project_id: str,
    thread_id: str,
    db_client,
) -> None:
    """Restore prior run-scoped thread artifacts into the current Claude workspace."""
    make_dir = getattr(bridge, "make_workspace_dir", None)
    write_file = getattr(bridge, "write_workspace_file_bytes", None)
    if not callable(write_file):
        logger.info(
            "[ClaudeSDKRunner] Bridge does not support thread workspace rehydrate"
        )
        return

    from services.workspace_artifacts import workspace_artifacts

    async def _make_dir(path: str) -> None:
        if callable(make_dir):
            await make_dir(path)

    async def _write_file(path: str, data: bytes) -> None:
        await write_file(path, data)

    try:
        result = await workspace_artifacts.rehydrate_thread(
            project_id=project_id,
            thread_id=thread_id,
            client=db_client,
            make_dir=_make_dir,
            write_file=_write_file,
        )
    except Exception as exc:
        logger.warning(
            "[ClaudeSDKRunner] Failed to rehydrate thread workspace: %s",
            exc,
        )
        return

    if result.get("total"):
        logger.info(
            "[ClaudeSDKRunner] Rehydrated thread workspace for thread=%s project=%s: %s/%s files restored",
            thread_id,
            project_id,
            result.get("rehydrated", 0),
            result.get("total", 0),
        )
