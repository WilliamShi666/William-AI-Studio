from __future__ import annotations

from typing import Optional
import uuid
import os
import asyncio
from contextlib import AsyncExitStack
import threading
import hashlib
import json
import posixpath
import shlex
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, TYPE_CHECKING, TypeVar

from agentpress.tool import Tool

# PPIO 沙箱类型 - 根据 sandbox_type 在 create_sandbox 中动态选择具体实现
# 支持: desktop (e2b-desktop), browser/code/base (e2b-code-interpreter)
from sandbox.sandbox import (
    create_sandbox,
    delete_sandbox,
    extract_sandbox_template_id,
    get_or_start_sandbox,
    resolve_sandbox_template_lineage,
    resume_or_create_sandbox,
)
from sandbox.session_control import (
    ShadowCloneSandboxSessionState,
    get_or_create_shadow_clone_session_state,
)
from services.workspace_artifacts import (
    is_user_visible_workspace_artifact_path,
    workspace_artifacts,
)
from utils.logger import logger
from utils.files_utils import clean_path
from utils.agent_run_context import get_agent_run_context
from utils.config import config
from services import redis, sandbox_capacity, sandbox_create_capacity

if TYPE_CHECKING:
    from agentpress.adk_thread_manager import ADKThreadManager

SANDBOX_ID_MAP_PREFIX = "sandbox:idmap:"
SANDBOX_ID_MAP_TTL = int(os.getenv("SANDBOX_ID_MAP_TTL", "3600"))
SANDBOX_CREATE_LOCK_PREFIX = "sandbox:create_lock:"


def _safe_positive_float(
    value: str | None, default: float, *, min_value: float = 0.1
) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else min_value


def _safe_positive_int(value: str | None, default: int, *, min_value: int = 1) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else min_value


def _safe_non_negative_float(value: str | None, default: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


SANDBOX_TOOL_CALL_TIMEOUT_SECONDS = _safe_positive_float(
    os.getenv("SANDBOX_TOOL_CALL_TIMEOUT_SECONDS")
    or os.getenv("SANDBOX_IO_TIMEOUT_SECONDS"),
    20.0,
)
SANDBOX_CONNECT_TIMEOUT_SECONDS = _safe_positive_float(
    os.getenv("SANDBOX_CONNECT_TIMEOUT_SECONDS"),
    20.0,
)
SANDBOX_CONNECT_RETRY_COUNT = _safe_positive_int(
    os.getenv("SANDBOX_CONNECT_RETRY_COUNT"),
    0,
    min_value=0,
)
SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS = _safe_positive_float(
    os.getenv("SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS"),
    0.4,
    min_value=0.0,
)
SANDBOX_TOOL_MAX_CONCURRENT_GLOBAL = _safe_positive_int(
    os.getenv("SANDBOX_TOOL_MAX_CONCURRENT_GLOBAL"),
    24,
)
SANDBOX_TOOL_MAX_CONCURRENT_PER_SANDBOX = _safe_positive_int(
    os.getenv("SANDBOX_TOOL_MAX_CONCURRENT_PER_SANDBOX"),
    10,
)
SANDBOX_TOOL_QUEUE_TIMEOUT_SECONDS = _safe_positive_float(
    os.getenv("SANDBOX_TOOL_QUEUE_TIMEOUT_SECONDS")
    or os.getenv("SANDBOX_IO_QUEUE_TIMEOUT_SECONDS"),
    3.0,
)
SANDBOX_CREATE_LOCK_TTL_SECONDS = _safe_positive_int(
    os.getenv("SANDBOX_CREATE_LOCK_TTL_SECONDS"),
    180,
    min_value=10,
)
SANDBOX_CREATE_LOCK_WAIT_TIMEOUT_SECONDS = _safe_positive_float(
    os.getenv("SANDBOX_CREATE_LOCK_WAIT_TIMEOUT_SECONDS"),
    240.0,
    min_value=1.0,
)
SANDBOX_CREATE_LOCK_POLL_SECONDS = _safe_positive_float(
    os.getenv("SANDBOX_CREATE_LOCK_POLL_SECONDS"),
    2.0,
    min_value=0.1,
)
SHADOW_CLONE_LEASE_WAIT_TIMEOUT_SECONDS = _safe_positive_float(
    os.getenv("SHADOW_CLONE_LEASE_WAIT_TIMEOUT_SECONDS"),
    15.0,
    min_value=0.0,
)
SHADOW_CLONE_LEASE_WAIT_POLL_SECONDS = _safe_positive_float(
    os.getenv("SHADOW_CLONE_LEASE_WAIT_POLL_SECONDS"),
    0.2,
    min_value=0.05,
)
CLAUDE_SKILLS_WORKSPACE_DIR = "/workspace/skills"
CLAUDE_SKILLS_BOOTSTRAP_TARGET = "/workspace/claude_skills_sandbox_bootstrap.py"
CLAUDE_SKILLS_BOOTSTRAP_MODE = "copy"
CLAUDE_SKILLS_BOOTSTRAP_METADATA = f"{CLAUDE_SKILLS_WORKSPACE_DIR}/skills_metadata.json"
CLAUDE_SKILLS_BOOTSTRAP_LOCAL_TIMEOUT_SECONDS = max(
    SANDBOX_TOOL_CALL_TIMEOUT_SECONDS,
    _safe_positive_float(
        os.getenv("CLAUDE_SKILLS_BOOTSTRAP_LOCAL_TIMEOUT_SECONDS"),
        300.0,
    ),
)
CLAUDE_SKILLS_BOOTSTRAP_REMOTE_TIMEOUT_SECONDS = _safe_non_negative_float(
    os.getenv("CLAUDE_SKILLS_BOOTSTRAP_REMOTE_TIMEOUT_SECONDS"),
    0.0,
)
WORKSPACE_ARTIFACTS_REHYDRATE_ON_RECOVERY = os.getenv(
    "WORKSPACE_ARTIFACTS_REHYDRATE_ON_RECOVERY", "true"
).lower() in {"1", "true", "yes", "on"}

_SEMAPHORE_ACQUIRE_POLL_SECONDS = 0.1
_tool_global_semaphore: Optional[threading.BoundedSemaphore] = None
_tool_per_sandbox_semaphores: Dict[str, threading.BoundedSemaphore] = {}
_tool_per_sandbox_lock = threading.Lock()

T = TypeVar("T")
_STATE_UNSET = object()
_SANDBOX_NOT_FOUND_MARKERS = (
    "sandbox not found",
    "sandbox was not found",
)
_SANDBOX_CREATE_LOCK_RELEASE_SCRIPT = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) "
    "end "
    "return 0"
)
_TERMINAL_AGENT_RUN_STATUSES = {"completed", "failed", "stopped", "error"}


class SandboxToolQueueTimeoutError(TimeoutError):
    def __init__(
        self,
        operation: str,
        timeout_seconds: float,
        *,
        sandbox_id: str,
        project_id: str,
    ) -> None:
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.sandbox_id = sandbox_id
        self.project_id = project_id
        super().__init__(
            f"Sandbox tool queue timed out after {timeout_seconds:.1f}s "
            f"(operation={operation}, project={project_id}, sandbox={sandbox_id})"
        )


class SandboxToolsBase(Tool):
    """All sandbox tools base class, provide sandbox access based on project."""

    # 类变量，跟踪是否已打印沙箱URL
    _urls_printed = False

    def __init__(
        self,
        project_id: str,
        thread_manager: Optional["ADKThreadManager"] = None,
        sandbox_type: str = "desktop",
        *,
        shadow_clone_run_id: Optional[str] = None,
        strict_sandbox: bool = False,
    ):
        super().__init__()
        self.project_id = project_id
        self.thread_manager = thread_manager
        self.sandbox_type = sandbox_type  # 沙箱类型
        self.workspace_path = "/workspace"
        self._sandbox = None
        self._sandbox_id = None
        self._sandbox_pass = None
        self._shadow_clone_run_id = str(shadow_clone_run_id or "").strip() or None
        self._strict_sandbox_mode = bool(strict_sandbox and self._shadow_clone_run_id)
        self._shared_shadow_clone_session: Optional[ShadowCloneSandboxSessionState] = (
            None
        )
        if self._strict_sandbox_mode and self._shadow_clone_run_id:
            self._shared_shadow_clone_session = (
                get_or_create_shadow_clone_session_state(
                    project_id=self.project_id,
                    run_id=self._shadow_clone_run_id,
                    sandbox_type=self.sandbox_type,
                )
            )
        self._ensure_lock = (
            self._shared_shadow_clone_session.ensure_lock
            if self._shared_shadow_clone_session is not None
            else asyncio.Lock()
        )
        self._workspace_sync_snapshot: Dict[str, str] = {}

    def _get_runtime_sandbox(self) -> Any:
        if self._shared_shadow_clone_session is not None:
            return self._shared_shadow_clone_session.sandbox
        return self._sandbox

    def _set_runtime_sandbox(self, sandbox_obj: Any) -> None:
        if self._shared_shadow_clone_session is not None:
            self._shared_shadow_clone_session.bind(sandbox=sandbox_obj)
            return
        self._sandbox = sandbox_obj

    def _clear_runtime_sandbox(self) -> None:
        if self._shared_shadow_clone_session is not None:
            self._shared_shadow_clone_session.clear_runtime_handle()
            return
        self._sandbox = None

    def _get_runtime_sandbox_id(self) -> Optional[str]:
        if self._shared_shadow_clone_session is not None:
            return self._shared_shadow_clone_session.sandbox_id
        return self._sandbox_id

    def _get_runtime_sandbox_pass(self) -> Optional[str]:
        if self._shared_shadow_clone_session is not None:
            return self._shared_shadow_clone_session.sandbox_pass
        return self._sandbox_pass

    def _bind_runtime_state(
        self,
        *,
        sandbox: Any = _STATE_UNSET,
        sandbox_id: Any = _STATE_UNSET,
        sandbox_pass: Any = _STATE_UNSET,
        sandbox_type: Any = _STATE_UNSET,
    ) -> None:
        shared = self._shared_shadow_clone_session
        if shared is not None:
            shared.bind(
                sandbox=None if sandbox is _STATE_UNSET else sandbox,
                sandbox_id=None if sandbox_id is _STATE_UNSET else sandbox_id,
                sandbox_pass=None if sandbox_pass is _STATE_UNSET else sandbox_pass,
                sandbox_type=None if sandbox_type is _STATE_UNSET else sandbox_type,
            )
        else:
            if sandbox is not _STATE_UNSET:
                self._sandbox = sandbox
            if sandbox_id is not _STATE_UNSET:
                self._sandbox_id = sandbox_id
            if sandbox_pass is not _STATE_UNSET:
                self._sandbox_pass = sandbox_pass
        if sandbox_type is not _STATE_UNSET and sandbox_type:
            self.sandbox_type = str(sandbox_type).strip() or self.sandbox_type

    async def _get_workspace_artifact_client(self) -> Any:
        if self.thread_manager is None or not hasattr(self.thread_manager, "db"):
            return None
        try:
            return await self.thread_manager.db.client
        except Exception as client_error:
            logger.warning(
                "Failed to acquire workspace artifact DB client for project %s: %s",
                self.project_id,
                client_error,
            )
            return None

    async def _current_agent_run_terminal_status(self) -> Optional[str]:
        agent_run_id, _ = get_agent_run_context()
        if not agent_run_id:
            return None
        client = await self._get_workspace_artifact_client()
        if client is None:
            return None
        try:
            result = (
                await client.table("agent_runs")
                .select("status")
                .eq("agent_run_id", agent_run_id)
                .limit(1)
                .execute()
            )
            rows = list(getattr(result, "data", None) or [])
            if not rows:
                return None
            status = str(rows[0].get("status") or "").strip().lower()
            return status if status in _TERMINAL_AGENT_RUN_STATUSES else None
        except Exception as terminal_check_error:
            logger.warning(
                "Failed to check terminal state before sandbox side effect run=%s project=%s: %s",
                agent_run_id,
                self.project_id,
                terminal_check_error,
            )
            return None

    async def _raise_if_agent_run_terminal(self, operation: str) -> None:
        status = await self._current_agent_run_terminal_status()
        if not status:
            return
        agent_run_id, _ = get_agent_run_context()
        raise RuntimeError(
            f"AGENT_RUN_TERMINAL_FENCE: refusing sandbox {operation} for "
            f"terminal agent_run_id={agent_run_id} status={status}"
        )

    async def _persist_workspace_artifact(
        self,
        path: str,
        content: Any,
        *,
        source: str,
        content_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        allow_hidden: bool = False,
    ) -> None:
        if not allow_hidden and not is_user_visible_workspace_artifact_path(path):
            return
        if await self._current_agent_run_terminal_status():
            return

        client = await self._get_workspace_artifact_client()
        agent_run_id, thread_id = get_agent_run_context()
        try:
            await workspace_artifacts.persist_artifact(
                project_id=self.project_id,
                path=path,
                content=content,
                source=source,
                client=client,
                thread_id=thread_id,
                agent_run_id=self._resolve_strict_shadow_clone_run_id() or agent_run_id,
                sandbox_id=self._get_runtime_sandbox_id(),
                metadata=metadata,
                content_type=content_type,
                allow_hidden=allow_hidden,
            )
        except Exception as persist_error:
            logger.warning(
                "Workspace artifact persist failed for project=%s path=%s source=%s: %s",
                self.project_id,
                path,
                source,
                persist_error,
            )

    async def _read_workspace_artifact_bytes(
        self,
        path: str,
        *,
        include_hidden: bool = False,
    ) -> Optional[bytes]:
        if not include_hidden and not is_user_visible_workspace_artifact_path(path):
            return None

        client = await self._get_workspace_artifact_client()
        try:
            artifact = await workspace_artifacts.read_artifact_bytes(
                project_id=self.project_id,
                path=path,
                client=client,
                include_hidden=include_hidden,
            )
        except Exception as read_error:
            logger.warning(
                "Workspace artifact read failed for project=%s path=%s: %s",
                self.project_id,
                path,
                read_error,
            )
            return None
        if artifact is None:
            return None
        return artifact[1]

    async def _list_workspace_artifact_entries(
        self,
        path: str,
        *,
        include_hidden: bool = False,
    ) -> list[Dict[str, Any]]:
        if not include_hidden and not is_user_visible_workspace_artifact_path(path):
            return []

        client = await self._get_workspace_artifact_client()
        try:
            return await workspace_artifacts.list_entries(
                project_id=self.project_id,
                path=path,
                client=client,
                include_hidden=include_hidden,
            )
        except Exception as list_error:
            logger.warning(
                "Workspace artifact list failed for project=%s path=%s: %s",
                self.project_id,
                path,
                list_error,
            )
            return []

    async def _rehydrate_workspace_artifacts(
        self, client: Any, *, sandbox_id: str
    ) -> None:
        if not WORKSPACE_ARTIFACTS_REHYDRATE_ON_RECOVERY:
            return

        async def _make_dir(path: str) -> None:
            await self._run_blocking_sandbox_call(
                "files.make_dir",
                lambda current=path: self.sandbox.files.make_dir(current),
            )

        async def _write_file(path: str, data: bytes) -> None:
            await self._run_blocking_sandbox_call(
                "files.write",
                lambda current=path, payload=data: self.sandbox.files.write(
                    current, payload
                ),
            )

        try:
            result = await workspace_artifacts.rehydrate_project(
                project_id=self.project_id,
                client=client,
                make_dir=_make_dir,
                write_file=_write_file,
            )
        except Exception as rehydrate_error:
            logger.warning(
                "Workspace artifact rehydrate failed for project=%s sandbox=%s: %s",
                self.project_id,
                sandbox_id,
                rehydrate_error,
            )
            return

        if result.get("total"):
            logger.info(
                "Workspace artifact rehydrate completed for project=%s sandbox=%s: %s/%s files restored",
                self.project_id,
                sandbox_id,
                result.get("rehydrated", 0),
                result.get("total", 0),
            )

    @staticmethod
    def _is_dir_entry(entry: Any) -> bool:
        if hasattr(entry, "is_dir"):
            return bool(getattr(entry, "is_dir"))
        if hasattr(entry, "isDir"):
            return bool(getattr(entry, "isDir"))
        if hasattr(entry, "type"):
            return getattr(entry, "type") == "directory"
        return False

    @staticmethod
    def _entry_mod_time(entry: Any) -> str:
        if hasattr(entry, "mod_time"):
            return str(getattr(entry, "mod_time"))
        if hasattr(entry, "modTime"):
            return str(getattr(entry, "modTime"))
        if hasattr(entry, "modified"):
            return str(getattr(entry, "modified"))
        return ""

    @staticmethod
    def _is_directory_like_read_error(error: Exception) -> bool:
        message = str(error or "").strip().lower()
        return "directory" in message or "is a dir" in message

    async def _collect_workspace_files_recursive(
        self, root_path: str = "/workspace"
    ) -> list[Dict[str, Any]]:
        collected: list[Dict[str, Any]] = []
        pending_dirs = [root_path]
        seen_dirs: set[str] = set()

        while pending_dirs:
            current_dir = pending_dirs.pop()
            if current_dir in seen_dirs:
                continue
            seen_dirs.add(current_dir)

            entries = await self._run_blocking_sandbox_call(
                "files.list",
                lambda current=current_dir: self.sandbox.files.list(current),
            )
            for entry in entries or []:
                name = str(getattr(entry, "name", "") or "")
                if not name:
                    continue
                full_path = (
                    f"{current_dir.rstrip('/')}/{name}"
                    if current_dir != "/"
                    else f"/{name}"
                )
                normalized_path = posixpath.normpath(full_path)
                if not is_user_visible_workspace_artifact_path(normalized_path):
                    continue

                if self._is_dir_entry(entry):
                    pending_dirs.append(normalized_path)
                    continue

                collected.append(
                    {
                        "path": normalized_path,
                        "size": int(getattr(entry, "size", 0) or 0),
                        "mod_time": self._entry_mod_time(entry),
                    }
                )

        return collected

    def _supports_workspace_artifact_sync(self) -> bool:
        if self._get_runtime_sandbox() is None:
            return False
        files_api = getattr(self.sandbox, "files", None)
        if files_api is None:
            return False
        return callable(getattr(files_api, "list", None)) and callable(
            getattr(files_api, "read", None)
        )

    async def _sync_workspace_artifacts_from_sandbox(
        self,
        *,
        source: str,
    ) -> None:
        client = await self._get_workspace_artifact_client()
        if client is None:
            return
        if not self._supports_workspace_artifact_sync():
            return

        try:
            files = await self._collect_workspace_files_recursive(self.workspace_path)
        except Exception as sync_error:
            logger.warning(
                "Workspace artifact sync list failed for project=%s sandbox=%s: %s",
                self.project_id,
                self._get_runtime_sandbox_id(),
                sync_error,
            )
            return

        live_paths: set[str] = set()
        next_snapshot: Dict[str, str] = {}
        for item in files:
            path = str(item.get("path") or "")
            if not path:
                continue
            live_paths.add(path)
            fingerprint = f"{item.get('size', 0)}:{item.get('mod_time') or ''}"
            next_snapshot[path] = fingerprint

            existing_record = None
            try:
                existing_record = await workspace_artifacts.get_artifact_record(
                    project_id=self.project_id,
                    path=path,
                    client=client,
                )
            except Exception as record_error:
                logger.debug(
                    "Workspace artifact record lookup failed for project=%s path=%s: %s",
                    self.project_id,
                    path,
                    record_error,
                )

            if (
                self._workspace_sync_snapshot.get(path) == fingerprint
                and existing_record is not None
            ):
                continue

            try:
                payload = await self._run_blocking_sandbox_call(
                    "files.read",
                    lambda current=path: self.sandbox.files.read(
                        current, format="bytes"
                    ),
                )
            except Exception as read_error:
                if self._is_directory_like_read_error(read_error):
                    logger.debug(
                        "Workspace artifact sync skipped directory-like path for project=%s path=%s: %s",
                        self.project_id,
                        path,
                        read_error,
                    )
                else:
                    logger.warning(
                        "Workspace artifact sync read failed for project=%s path=%s: %s",
                        self.project_id,
                        path,
                        read_error,
                    )
                continue

            if isinstance(payload, (bytes, bytearray)):
                data = bytes(payload)
            else:
                data = str(payload or "").encode("utf-8")

            content_sha = hashlib.sha256(data).hexdigest()
            if existing_record is not None and existing_record.sha256 == content_sha:
                continue

            await self._persist_workspace_artifact(
                path,
                data,
                source=source,
            )

        try:
            existing_paths = await workspace_artifacts.collect_file_paths(
                project_id=self.project_id,
                root_path=self.workspace_path,
                client=client,
            )
        except Exception as collect_error:
            logger.debug(
                "Workspace artifact path collection failed for project=%s: %s",
                self.project_id,
                collect_error,
            )
            existing_paths = []

        missing_paths = set(existing_paths) - live_paths
        for missing_path in missing_paths:
            try:
                await workspace_artifacts.delete_artifact(
                    project_id=self.project_id,
                    path=missing_path,
                    client=client,
                )
            except Exception as delete_error:
                logger.debug(
                    "Workspace artifact delete failed for project=%s path=%s: %s",
                    self.project_id,
                    missing_path,
                    delete_error,
                )

        self._workspace_sync_snapshot = next_snapshot

    def _get_global_tool_semaphore(self) -> threading.BoundedSemaphore:
        global _tool_global_semaphore
        with _tool_per_sandbox_lock:
            if _tool_global_semaphore is None:
                _tool_global_semaphore = threading.BoundedSemaphore(
                    SANDBOX_TOOL_MAX_CONCURRENT_GLOBAL
                )
            return _tool_global_semaphore

    def _get_per_sandbox_semaphore(self) -> threading.BoundedSemaphore:
        semaphore_key = str(self._get_runtime_sandbox_id() or self.project_id)
        with _tool_per_sandbox_lock:
            semaphore = _tool_per_sandbox_semaphores.get(semaphore_key)
            if semaphore is None:
                semaphore = threading.BoundedSemaphore(
                    SANDBOX_TOOL_MAX_CONCURRENT_PER_SANDBOX
                )
                _tool_per_sandbox_semaphores[semaphore_key] = semaphore
            return semaphore

    @staticmethod
    async def _acquire_capacity_permit(
        semaphore: threading.BoundedSemaphore,
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        deadline = (
            None
            if timeout_seconds is None
            else time.monotonic() + max(0.0, timeout_seconds)
        )

        while True:
            if semaphore.acquire(blocking=False):
                return True

            if deadline is None:
                wait_seconds = _SEMAPHORE_ACQUIRE_POLL_SECONDS
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                wait_seconds = min(_SEMAPHORE_ACQUIRE_POLL_SECONDS, remaining)
            await asyncio.sleep(wait_seconds)

    async def _update_sandbox_state(self, sandbox_info: dict, **updates) -> None:
        """Merge updates into sandbox JSONB and persist to projects table."""
        merged = {**sandbox_info, **updates}
        client = await self.thread_manager.db.client
        await client.table("projects").update({"sandbox": json.dumps(merged)}).eq(
            "project_id", self.project_id
        ).execute()

    @staticmethod
    def _is_sandbox_not_found_error(error: Exception) -> bool:
        lowered = str(error).lower()
        return any(marker in lowered for marker in _SANDBOX_NOT_FOUND_MARKERS)

    @staticmethod
    def _parse_sandbox_info(raw_sandbox: Any) -> dict:
        if isinstance(raw_sandbox, str):
            try:
                return json.loads(raw_sandbox) if raw_sandbox.strip() else {}
            except json.JSONDecodeError:
                return {}
        if isinstance(raw_sandbox, dict):
            return raw_sandbox
        return {}

    @staticmethod
    def _normalize_template_value(value: Any) -> Optional[str]:
        candidate = str(value or "").strip()
        return candidate or None

    @classmethod
    def _build_template_lineage(
        cls,
        sandbox_type: str,
        *,
        sandbox_obj: Any = None,
        fallback_template_id: Optional[str] = None,
        allow_expected_fallback: bool = True,
    ) -> Dict[str, str]:
        lineage = resolve_sandbox_template_lineage(sandbox_type)
        resolved_template_id = cls._normalize_template_value(
            extract_sandbox_template_id(sandbox_obj)
            or fallback_template_id
            or (lineage.get("template_id") if allow_expected_fallback else None)
        )
        return {
            "template_type": str(
                lineage.get("template_type") or sandbox_type or "desktop"
            ),
            "template_source": str(lineage.get("template_source") or "config_env"),
            "template_id": resolved_template_id
            or (
                str(lineage.get("template_id") or "") if allow_expected_fallback else ""
            ),
        }

    @classmethod
    def _merge_template_lineage(
        cls,
        sandbox_info: dict,
        *,
        sandbox_type: str,
        sandbox_obj: Any = None,
        fallback_template_id: Optional[str] = None,
        allow_expected_fallback: bool = True,
    ) -> dict:
        lineage = cls._build_template_lineage(
            sandbox_type,
            sandbox_obj=sandbox_obj,
            fallback_template_id=fallback_template_id,
            allow_expected_fallback=allow_expected_fallback,
        )
        merged = dict(sandbox_info)
        merged.update(lineage)
        return merged

    @classmethod
    def _get_template_drift(
        cls,
        sandbox_info: dict,
        *,
        sandbox_type: str,
        sandbox_obj: Any = None,
    ) -> Optional[Dict[str, str]]:
        expected = cls._build_template_lineage(sandbox_type)
        actual_template_id = cls._normalize_template_value(
            extract_sandbox_template_id(sandbox_obj) or sandbox_info.get("template_id")
        )
        if not actual_template_id:
            return None

        expected_template_id = cls._normalize_template_value(
            expected.get("template_id")
        )
        if not expected_template_id or actual_template_id == expected_template_id:
            return None

        return {
            "expected_template_id": expected_template_id,
            "actual_template_id": actual_template_id,
            "template_type": str(
                expected.get("template_type") or sandbox_type or "desktop"
            ),
        }

    @staticmethod
    def _extract_sandbox_id(sandbox_obj: Any, fallback: Optional[str] = None) -> str:
        return str(
            getattr(sandbox_obj, "sandbox_id", None)
            or getattr(sandbox_obj, "id", None)
            or fallback
            or ""
        ).strip()

    @staticmethod
    def _extract_stream_url(vnc_url_raw: Any) -> Optional[str]:
        if isinstance(vnc_url_raw, list):
            return vnc_url_raw[0] if vnc_url_raw else None
        if hasattr(vnc_url_raw, "url"):
            return str(vnc_url_raw.url or "") or None
        if vnc_url_raw:
            return str(vnc_url_raw)
        return None

    @staticmethod
    def _shadow_clone_manifest_requires_claude_skills(
        lease: Optional[Dict[str, Any]],
    ) -> bool:
        manifest = (lease or {}).get("environment_manifest")
        if not isinstance(manifest, dict):
            return False
        if (
            str(manifest.get("prepared_by") or "").strip()
            == "shadow_clone_preconfirm_prepare"
        ):
            return True
        bootstrap = manifest.get("bootstrap")
        if (
            isinstance(bootstrap, dict)
            and str(bootstrap.get("status") or "").strip().lower() == "ready"
        ):
            return True
        skills = manifest.get("skills")
        return bool(isinstance(skills, dict) and skills.get("ready"))

    @staticmethod
    def _local_claude_skills_bootstrap_path() -> Optional[Path]:
        repo_root = Path(__file__).resolve().parents[2]
        path = repo_root / "claude_skills_sandbox_bootstrap.py"
        return path if path.exists() else None

    async def _sandbox_path_exists(
        self, path: str, *, expect_dir: bool = False
    ) -> bool:
        operation = "files.list" if expect_dir else "files.read"
        try:
            if expect_dir:
                await self._run_blocking_sandbox_call(
                    operation,
                    lambda current=path: self.sandbox.files.list(current),
                )
            else:
                await self._run_blocking_sandbox_call(
                    operation,
                    lambda current=path: self.sandbox.files.read(current),
                )
            return True
        except Exception:
            return False

    @staticmethod
    @staticmethod
    def _create_skills_tar(skills_source: Path) -> Path:
        """Create a filtered tar.gz of claude_skills/ excluding non-skill files.

        Excludes (matching Dockerfile cleanup):
        - workspace directories and *-workspace sufﬁxed dirs
        - __pycache__, .claude/, .playwright-mcp/
        - minimax_skills_backup/
        - hero.png, landing-page-current.png, skill-creator-ultimate.skill
        - marketing-mode-guide.md, SKILLS_README.md, skill_agent.py
        - Old courseware dirs (american_civil_war_*, vectors_planes_*)
        """
        import tarfile
        import tempfile

        exclude_names = {
            "workspace",
            "__pycache__",
            ".claude",
            ".playwright-mcp",
            "minimax_skills_backup",
            "hero.png",
            "landing-page-current.png",
            "skill-creator-ultimate.skill",
            "marketing-mode-guide.md",
            "SKILLS_README.md",
            "skill_agent.py",
            "american_civil_war_ppt",
            "american_civil_war_ppt_v2",
            "american_civil_war_qwen",
            "american_civil_war_v4",
            "vectors_planes_courseware",
            "vectors_planes_v2",
            # Old skill dirs — replaced by *-ultimate symlinks (Dockerfile lines 267-270)
            "pdf",
            "docx",
            "pptx",
            "xlsx",
        }

        tar_fd, tar_path = tempfile.mkstemp(suffix=".tar.gz")
        os.close(tar_fd)

        with tarfile.open(tar_path, "w:gz") as tar:
            for item in sorted(skills_source.iterdir()):
                if item.name in exclude_names:
                    continue
                if item.name.endswith("-workspace"):
                    continue
                tar.add(item, arcname=item.name)

        return Path(tar_path)

    async def _ensure_claude_skills_from_local(self, *, force: bool = False) -> None:
        """Upload skills from backend/claude_skills/ to /workspace/skills/ in sandbox.

        Used when SKILL_EXPERIMENT_MODE=true — skills come from the local
        filesystem instead of the sandbox template, enabling instant iteration
        without template rebuilds.
        """
        skills_workspace_dir = CLAUDE_SKILLS_WORKSPACE_DIR
        metadata_path = f"{skills_workspace_dir}/skills_metadata.json"

        # Idempotency: if skills already exist with valid metadata, skip
        # even when force=True, because rmtree on the sandbox filesystem
        # can fail with ENOTEMPTY when files are held open.
        if await self._sandbox_path_exists(
            skills_workspace_dir, expect_dir=True
        ) and await self._sandbox_path_exists(metadata_path):
            logger.info("Skills already present in sandbox, skipping local upload")
            return

        skills_source = Path(__file__).resolve().parents[1] / "claude_skills"
        if not skills_source.exists():
            raise FileNotFoundError(
                f"claude_skills directory not found at {skills_source}"
            )

        logger.info(
            "Uploading skills from local filesystem (%s) to sandbox — experiment mode",
            skills_source,
        )

        # 1. Create filtered tar.gz
        tar_path = self._create_skills_tar(skills_source)
        try:
            tar_data = tar_path.read_bytes()
            logger.debug("Skills tar created: %s bytes", len(tar_data))

            # Use a unique name to avoid races between parallel sandbox inits
            import uuid

            tar_sandbox_name = f"/workspace/skills_source_{uuid.uuid4().hex[:8]}.tar.gz"
            source_dir_name = f"/workspace/skills_source_{uuid.uuid4().hex[:8]}"

            # 2. Upload to sandbox (use longer timeout for large tar)
            await self._run_blocking_sandbox_call(
                "files.write",
                lambda payload=tar_data, name=tar_sandbox_name: self.sandbox.files.write(
                    name, payload
                ),
            )

            # 3. Extract
            await self._run_blocking_sandbox_call(
                "commands.run",
                lambda sd=source_dir_name, tn=tar_sandbox_name: self.sandbox.commands.run(
                    f"mkdir -p {shlex.quote(sd)} && "
                    f"tar -xzf {shlex.quote(tn)} -C {shlex.quote(sd)}/"
                ),
            )

            # 3.5 Create symlinks so nested skills are discoverable
            symlinks = [
                ("document_skills_ultimate/docx-ultimate", "docx-ultimate"),
                ("document_skills_ultimate/pdf-ultimate", "pdf-ultimate"),
                ("document_skills_ultimate/pptx-ultimate", "pptx-ultimate"),
                ("document_skills_ultimate/xlsx-ultimate", "xlsx-ultimate"),
                ("minimax_skills/skills/android-native-dev", "android-native-dev"),
                ("minimax_skills/skills/ios-application-dev", "ios-application-dev"),
            ]
            for target, link_name in symlinks:
                await self._run_blocking_sandbox_call(
                    "commands.run",
                    lambda t=target, l=link_name, sd=source_dir_name: self.sandbox.commands.run(
                        f"ln -sfn {shlex.quote(sd + '/' + t)} "
                        f"{shlex.quote(sd + '/' + l)}"
                    ),
                )

            # 4. Upload bootstrap script and run it with custom source
            bootstrap_path = self._local_claude_skills_bootstrap_path()
            if bootstrap_path is None:
                raise FileNotFoundError(
                    "claude_skills_sandbox_bootstrap.py not found in repo root"
                )

            bootstrap_bytes = bootstrap_path.read_bytes()
            await self._run_blocking_sandbox_call(
                "files.write",
                lambda payload=bootstrap_bytes: self.sandbox.files.write(
                    CLAUDE_SKILLS_BOOTSTRAP_TARGET, payload
                ),
            )

            bootstrap_mode = CLAUDE_SKILLS_BOOTSTRAP_MODE
            remote_timeout = CLAUDE_SKILLS_BOOTSTRAP_REMOTE_TIMEOUT_SECONDS
            result = await self._run_blocking_sandbox_call(
                "commands.run",
                lambda cmd=(
                    f"python3 {shlex.quote(CLAUDE_SKILLS_BOOTSTRAP_TARGET)} "
                    f"--source {shlex.quote(source_dir_name)} "
                    f"--dest {shlex.quote(skills_workspace_dir)} "
                    f"--mode {shlex.quote(bootstrap_mode)} --force"
                ), remote_timeout=(
                    remote_timeout if remote_timeout > 0 else None
                ): self.sandbox.commands.run(
                    cmd,
                    timeout=remote_timeout,
                ),
                timeout_seconds=CLAUDE_SKILLS_BOOTSTRAP_LOCAL_TIMEOUT_SECONDS,
            )

            exit_code = getattr(result, "exit_code", 0)
            if exit_code != 0:
                stdout = str(getattr(result, "stdout", "") or "").strip()
                stderr = str(getattr(result, "stderr", "") or "").strip()
                raise RuntimeError(
                    f"Skills bootstrap (local mode) failed: {stdout} {stderr}".strip()
                )

            # 5. Verify metadata was generated
            if not await self._sandbox_path_exists(metadata_path):
                raise RuntimeError(
                    "Skills bootstrap (local mode) completed "
                    "without restoring skills metadata"
                )

            logger.info("Skills bootstrap (local mode) completed successfully")
        finally:
            # Always clean up local tar
            try:
                tar_path.unlink(missing_ok=True)
            except Exception:
                pass
            # Always clean up sandbox temp files
            try:
                await self._run_blocking_sandbox_call(
                    "commands.run",
                    lambda sd=source_dir_name, tn=tar_sandbox_name: self.sandbox.commands.run(
                        f"rm -rf {shlex.quote(sd)} {shlex.quote(tn)}"
                    ),
                )
            except Exception:
                pass

    async def _ensure_claude_skills_runtime_ready(
        self,
        *,
        force: bool = False,
        lease: Optional[Dict[str, Any]] = None,
    ) -> None:
        # --- EXPERIMENT MODE: Upload skills from local filesystem ---
        # Must be checked BEFORE the manifest gate, because the manifest
        # gate returns early when shadow_clone_mode=off (which is the
        # normal case for interactive agent runs).  In experiment mode
        # we always want to provision skills from the local source,
        # regardless of shadow clone state.
        if getattr(config, "SKILL_EXPERIMENT_MODE", False):
            return await self._ensure_claude_skills_from_local(force=force)

        # --- PRODUCTION MODE: Existing template-baked path below ---
        if not force and not self._shadow_clone_manifest_requires_claude_skills(lease):
            return

        manifest = (lease or {}).get("environment_manifest")
        if not isinstance(manifest, dict):
            manifest = {}
        bootstrap = manifest.get("bootstrap")
        if not isinstance(bootstrap, dict):
            bootstrap = {}
        skills = manifest.get("skills")
        if not isinstance(skills, dict):
            skills = {}

        skills_workspace_dir = (
            str(skills.get("workspace_dir") or CLAUDE_SKILLS_WORKSPACE_DIR).strip()
            or CLAUDE_SKILLS_WORKSPACE_DIR
        )
        bootstrap_target = (
            str(bootstrap.get("target") or CLAUDE_SKILLS_BOOTSTRAP_TARGET).strip()
            or CLAUDE_SKILLS_BOOTSTRAP_TARGET
        )
        bootstrap_mode = (
            str(bootstrap.get("mode") or CLAUDE_SKILLS_BOOTSTRAP_MODE).strip()
            or CLAUDE_SKILLS_BOOTSTRAP_MODE
        )
        bootstrap_metadata = (
            str(
                bootstrap.get("metadata_path")
                or f"{skills_workspace_dir}/skills_metadata.json"
            ).strip()
            or f"{skills_workspace_dir}/skills_metadata.json"
        )

        if await self._sandbox_path_exists(
            skills_workspace_dir, expect_dir=True
        ) and await self._sandbox_path_exists(bootstrap_metadata):
            return

        bootstrap_path = self._local_claude_skills_bootstrap_path()
        if bootstrap_path is None:
            raise FileNotFoundError(
                "claude_skills_sandbox_bootstrap.py not found in repo root"
            )

        bootstrap_bytes = bootstrap_path.read_bytes()
        await self._run_blocking_sandbox_call(
            "files.write",
            lambda current=bootstrap_target, payload=bootstrap_bytes: self.sandbox.files.write(
                current, payload
            ),
        )
        await self._persist_workspace_artifact(
            bootstrap_target,
            bootstrap_bytes,
            source="shadow_clone.strict_attach.skills_bootstrap",
            allow_hidden=True,
        )

        result = await self._run_blocking_sandbox_call(
            "commands.run",
            lambda cmd=(
                f"python3 {shlex.quote(bootstrap_target)} "
                f"--mode {shlex.quote(bootstrap_mode)} --force"
            ), remote_timeout=CLAUDE_SKILLS_BOOTSTRAP_REMOTE_TIMEOUT_SECONDS: self.sandbox.commands.run(
                cmd,
                timeout=remote_timeout,
            ),
            timeout_seconds=CLAUDE_SKILLS_BOOTSTRAP_LOCAL_TIMEOUT_SECONDS,
        )
        exit_code = getattr(result, "exit_code", 0)
        if exit_code != 0:
            stdout = str(getattr(result, "stdout", "") or "").strip()
            stderr = str(getattr(result, "stderr", "") or "").strip()
            raise RuntimeError(
                f"Shadow Clone skills bootstrap failed: {stdout} {stderr}".strip()
            )
        if not await self._sandbox_path_exists(bootstrap_metadata):
            raise RuntimeError(
                "Shadow Clone skills bootstrap completed without restoring skills metadata"
            )

    async def _connect_sandbox_with_retry(
        self, sandbox_id: str, sandbox_type: str
    ) -> Any:
        connect_attempts = max(1, SANDBOX_CONNECT_RETRY_COUNT + 1)
        last_error: Optional[Exception] = None

        for attempt in range(1, connect_attempts + 1):
            try:
                return await asyncio.wait_for(
                    get_or_start_sandbox(sandbox_id, sandbox_type),
                    timeout=SANDBOX_CONNECT_TIMEOUT_SECONDS,
                )
            except Exception as connect_error:
                last_error = connect_error
                logger.warning(
                    "Sandbox attach attempt %s/%s failed for project %s sandbox %s (%s): %s",
                    attempt,
                    connect_attempts,
                    self.project_id,
                    sandbox_id,
                    sandbox_type,
                    connect_error,
                )
                if (
                    attempt < connect_attempts
                    and SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS > 0
                ):
                    await asyncio.sleep(SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS * attempt)

        if last_error:
            raise last_error
        raise RuntimeError(f"Sandbox {sandbox_id} connection failed for unknown reason")

    async def _finalize_recovered_sandbox(
        self,
        client: Any,
        sandbox_info: dict,
        sandbox_obj: Any,
        *,
        sandbox_type: str,
        action: str,
        password: Optional[str],
        old_sandbox_id: Optional[str],
        migrated_from_type: Optional[str] = None,
    ) -> None:
        new_sandbox_id = self._extract_sandbox_id(sandbox_obj, old_sandbox_id)
        now_iso = datetime.now(timezone.utc).isoformat()
        sandbox_metadata = {
            **sandbox_info,
            "id": new_sandbox_id,
            "type": sandbox_type,
            "token": None,
            "mode": "desktop_realtime" if sandbox_type == "desktop" else None,
            "state": "running",
        }
        sandbox_metadata.pop("paused_at", None)

        if action == "created":
            sandbox_metadata["created_at"] = now_iso
        if password and (action == "created" or not sandbox_metadata.get("pass")):
            sandbox_metadata["pass"] = password

        if sandbox_type == "desktop":
            vnc_url = sandbox_metadata.get("vnc_preview") or sandbox_metadata.get(
                "sandbox_url"
            )
            try:
                await self._run_blocking_sandbox_call(
                    "stream.start",
                    lambda: sandbox_obj.stream.start(),
                )
            except Exception as start_error:
                if "already running" not in str(start_error).lower():
                    logger.warning(
                        f"Desktop stream start after recovery: {start_error}"
                    )

            try:
                vnc_url_raw = await self._run_blocking_sandbox_call(
                    "stream.get_url",
                    lambda: sandbox_obj.stream.get_url(),
                )
                vnc_url = self._extract_stream_url(vnc_url_raw)
            except Exception as stream_error:
                logger.warning(
                    f"desktop stream get url error (non-fatal): {stream_error}"
                )

            sandbox_metadata["vnc_preview"] = vnc_url
            sandbox_metadata["sandbox_url"] = vnc_url
        else:
            sandbox_metadata["vnc_preview"] = None
            sandbox_metadata["sandbox_url"] = None

        if migrated_from_type:
            sandbox_metadata["migrated_from"] = migrated_from_type
        else:
            sandbox_metadata.pop("migrated_from", None)

        sandbox_metadata = self._merge_template_lineage(
            sandbox_metadata,
            sandbox_type=sandbox_type,
            sandbox_obj=sandbox_obj,
            fallback_template_id=self._normalize_template_value(
                sandbox_info.get("template_id")
            ),
            allow_expected_fallback=(action == "created"),
        )

        await client.table("projects").eq("project_id", self.project_id).update(
            {"sandbox": json.dumps(sandbox_metadata)}
        )

        if old_sandbox_id and new_sandbox_id and new_sandbox_id != old_sandbox_id:
            try:
                await redis.set(
                    f"{SANDBOX_ID_MAP_PREFIX}{old_sandbox_id}",
                    new_sandbox_id,
                    ex=SANDBOX_ID_MAP_TTL,
                )
            except Exception as map_error:
                logger.warning(
                    f"Failed to store sandbox id mapping {old_sandbox_id} -> {new_sandbox_id}: {map_error}"
                )

        self._bind_runtime_state(
            sandbox=sandbox_obj,
            sandbox_id=new_sandbox_id,
            sandbox_pass=str(sandbox_metadata.get("pass") or password or ""),
            sandbox_type=sandbox_type,
        )

        should_rehydrate = bool(
            WORKSPACE_ARTIFACTS_REHYDRATE_ON_RECOVERY
            and new_sandbox_id
            and (
                action == "created"
                or (old_sandbox_id and new_sandbox_id != old_sandbox_id)
            )
        )
        if should_rehydrate:
            await self._rehydrate_workspace_artifacts(client, sandbox_id=new_sandbox_id)

    async def _load_project_sandbox_info(self, client) -> dict:
        project = (
            await client.table("projects")
            .select("*")
            .eq("project_id", self.project_id)
            .execute()
        )
        if not project.data or len(project.data) == 0:
            raise ValueError(f"Project {self.project_id} not found")

        project_data = project.data[0]
        return self._parse_sandbox_info(project_data.get("sandbox", "{}"))

    async def _create_sandbox_with_capacity(
        self,
        *,
        sandbox_pass: str,
        sandbox_type: str,
    ) -> Any:
        queue_timeout_seconds = (
            sandbox_create_capacity.get_sandbox_create_queue_timeout_seconds()
        )
        lease_ttl_seconds = (
            sandbox_create_capacity.compute_sandbox_create_lease_ttl_seconds(
                operation_timeout_seconds=SANDBOX_CONNECT_TIMEOUT_SECONDS,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        )
        async with sandbox_create_capacity.acquire_sandbox_create_capacity(
            holder_kind="tool_base_create",
            project_id=self.project_id,
            sandbox_type=sandbox_type,
            queue_timeout_seconds=queue_timeout_seconds,
            lease_ttl_seconds=lease_ttl_seconds,
        ):
            return await create_sandbox(
                sandbox_pass,
                self.project_id,
                sandbox_type,
            )

    async def _wait_for_sandbox_or_acquire_creation_lock(
        self, client, redis_client
    ) -> tuple[dict, str, Optional[str]]:
        lock_key = f"{SANDBOX_CREATE_LOCK_PREFIX}{self.project_id}"
        deadline = time.monotonic() + SANDBOX_CREATE_LOCK_WAIT_TIMEOUT_SECONDS
        sandbox_info = await self._load_project_sandbox_info(client)

        while not sandbox_info.get("id"):
            lock_owner_token = str(uuid.uuid4())
            lock_acquired = await redis_client.set(
                lock_key,
                lock_owner_token,
                nx=True,
                ex=SANDBOX_CREATE_LOCK_TTL_SECONDS,
            )
            if lock_acquired:
                sandbox_info = await self._load_project_sandbox_info(client)
                return sandbox_info, lock_key, lock_owner_token

            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out waiting for sandbox creation lock for project {self.project_id}",
                )

            await asyncio.sleep(SANDBOX_CREATE_LOCK_POLL_SECONDS)
            sandbox_info = await self._load_project_sandbox_info(client)

        return sandbox_info, lock_key, None

    async def _release_sandbox_creation_lock(
        self, redis_client, lock_key: str, lock_owner_token: str
    ) -> None:
        if not redis_client or not lock_key or not lock_owner_token:
            return

        try:
            if hasattr(redis_client, "eval"):
                await redis_client.eval(
                    _SANDBOX_CREATE_LOCK_RELEASE_SCRIPT,
                    1,
                    lock_key,
                    lock_owner_token,
                )
                return

            current_owner = await redis_client.get(lock_key)
            if isinstance(current_owner, bytes):
                current_owner = current_owner.decode("utf-8", errors="replace")
            if current_owner == lock_owner_token:
                await redis_client.delete(lock_key)
        except Exception as release_error:
            logger.warning(
                "Failed to release sandbox creation lock for project %s: %s",
                self.project_id,
                release_error,
            )

    async def _run_blocking_sandbox_call_once(
        self,
        operation: str,
        call: Callable[[], T],
        *,
        timeout_seconds: float,
        attempt_tag: str,
    ) -> T:
        global_semaphore = self._get_global_tool_semaphore()
        sandbox_semaphore = self._get_per_sandbox_semaphore()
        start = time.monotonic()
        sandbox_id = self._get_runtime_sandbox_id() or "unknown"
        queue_timeout_seconds = SANDBOX_TOOL_QUEUE_TIMEOUT_SECONDS

        try:
            async with AsyncExitStack() as exit_stack:
                use_process_local_per_sandbox_guard = (
                    not sandbox_capacity.is_sandbox_per_sandbox_capacity_enabled()
                )
                if not use_process_local_per_sandbox_guard:
                    try:
                        await exit_stack.enter_async_context(
                            sandbox_capacity.acquire_sandbox_per_sandbox_capacity(
                                holder_kind="tool_call",
                                sandbox_id=sandbox_id,
                                operation=operation,
                                queue_timeout_seconds=queue_timeout_seconds,
                                lease_ttl_seconds=sandbox_capacity.compute_sandbox_capacity_lease_ttl_seconds(
                                    operation_timeout_seconds=timeout_seconds,
                                    queue_timeout_seconds=queue_timeout_seconds,
                                ),
                                retain_lease_on_exceptions=(
                                    asyncio.TimeoutError,
                                    asyncio.CancelledError,
                                ),
                            )
                        )
                    except (
                        sandbox_capacity.SandboxPerSandboxCapacityTimeoutError
                    ) as capacity_timeout_error:
                        raise SandboxToolQueueTimeoutError(
                            operation,
                            queue_timeout_seconds,
                            sandbox_id=sandbox_id,
                            project_id=self.project_id,
                        ) from capacity_timeout_error
                    except (
                        sandbox_capacity.SandboxPerSandboxCapacityBackendUnavailableError
                    ) as capacity_error:
                        use_process_local_per_sandbox_guard = True
                        logger.warning(
                            "Shared sandbox per-sandbox capacity unavailable during %s for project %s "
                            "sandbox %s; falling back to process-local tool limiter: %s",
                            operation,
                            self.project_id,
                            sandbox_id,
                            capacity_error,
                        )

                if use_process_local_per_sandbox_guard:
                    sandbox_acquired = await self._acquire_capacity_permit(
                        sandbox_semaphore,
                        timeout_seconds=queue_timeout_seconds,
                    )
                    if not sandbox_acquired:
                        raise SandboxToolQueueTimeoutError(
                            operation,
                            queue_timeout_seconds,
                            sandbox_id=sandbox_id,
                            project_id=self.project_id,
                        )
                    exit_stack.callback(sandbox_semaphore.release)

                use_process_local_global_guard = (
                    not sandbox_capacity.is_sandbox_global_capacity_enabled()
                )

                if not use_process_local_global_guard:
                    try:
                        await exit_stack.enter_async_context(
                            sandbox_capacity.acquire_sandbox_global_capacity(
                                holder_kind="tool_call",
                                sandbox_id=sandbox_id,
                                operation=operation,
                                queue_timeout_seconds=queue_timeout_seconds,
                                lease_ttl_seconds=sandbox_capacity.compute_sandbox_capacity_lease_ttl_seconds(
                                    operation_timeout_seconds=timeout_seconds,
                                    queue_timeout_seconds=queue_timeout_seconds,
                                ),
                                retain_lease_on_exceptions=(
                                    asyncio.TimeoutError,
                                    asyncio.CancelledError,
                                ),
                            )
                        )
                    except (
                        sandbox_capacity.SandboxGlobalCapacityTimeoutError
                    ) as capacity_timeout_error:
                        raise SandboxToolQueueTimeoutError(
                            operation,
                            queue_timeout_seconds,
                            sandbox_id=sandbox_id,
                            project_id=self.project_id,
                        ) from capacity_timeout_error
                    except (
                        sandbox_capacity.SandboxGlobalCapacityBackendUnavailableError
                    ) as capacity_error:
                        use_process_local_global_guard = True
                        logger.warning(
                            "Shared sandbox global capacity unavailable during %s for project %s "
                            "sandbox %s; falling back to process-local tool limiter: %s",
                            operation,
                            self.project_id,
                            sandbox_id,
                            capacity_error,
                        )

                if use_process_local_global_guard:
                    global_acquired = await self._acquire_capacity_permit(
                        global_semaphore,
                        timeout_seconds=queue_timeout_seconds,
                    )
                    if not global_acquired:
                        raise SandboxToolQueueTimeoutError(
                            operation,
                            queue_timeout_seconds,
                            sandbox_id=sandbox_id,
                            project_id=self.project_id,
                        )
                    exit_stack.callback(global_semaphore.release)

                return await asyncio.wait_for(
                    asyncio.to_thread(call),
                    timeout=timeout_seconds,
                )
        except asyncio.TimeoutError as timeout_error:
            raise TimeoutError(
                f"Sandbox operation '{operation}' timed out after {timeout_seconds:.1f}s "
                f"(project={self.project_id}, sandbox={sandbox_id})"
            ) from timeout_error
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            logger.debug(
                "Sandbox tool call finished",
                operation=operation,
                project_id=self.project_id,
                sandbox_id=self._get_runtime_sandbox_id(),
                elapsed_ms=round(elapsed_ms, 2),
                attempt=attempt_tag,
            )

    async def _run_blocking_sandbox_call(
        self,
        operation: str,
        call: Callable[[], T],
        *,
        timeout_seconds: float | None = None,
    ) -> T:
        await self._raise_if_agent_run_terminal(operation)
        effective_timeout = timeout_seconds or SANDBOX_TOOL_CALL_TIMEOUT_SECONDS
        try:
            return await self._run_blocking_sandbox_call_once(
                operation,
                call,
                timeout_seconds=effective_timeout,
                attempt_tag="initial",
            )
        except Exception as first_error:
            if not self._is_sandbox_not_found_error(first_error):
                raise

            agent_run_id, _ = get_agent_run_context()
            logger.warning(
                "Sandbox stale-handle detected run=%s project=%s operation=%s sandbox=%s. "
                "Resetting handle and retrying once.",
                agent_run_id,
                self.project_id,
                operation,
                self._get_runtime_sandbox_id(),
            )
            self._clear_runtime_sandbox()
            await self._raise_if_agent_run_terminal(operation)
            await self._ensure_sandbox()
            return await self._run_blocking_sandbox_call_once(
                operation,
                call,
                timeout_seconds=effective_timeout,
                attempt_tag="self_heal_retry",
            )

    def _resolve_strict_shadow_clone_run_id(self) -> Optional[str]:
        if self._shadow_clone_run_id:
            return self._shadow_clone_run_id
        agent_run_id, _ = get_agent_run_context()
        return agent_run_id

    async def _await_shadow_clone_bound_lease(
        self, run_id: str
    ) -> Optional[Dict[str, Any]]:
        from agentscope_integration.shadow_clone.sandbox_lease import (
            get_run_sandbox_lease,
        )

        wait_timeout_seconds = SHADOW_CLONE_LEASE_WAIT_TIMEOUT_SECONDS
        lease = await get_run_sandbox_lease(run_id)
        if lease is not None or wait_timeout_seconds <= 0:
            return lease

        deadline = time.monotonic() + wait_timeout_seconds
        logger.info(
            "[ShadowClone] Waiting for strict sandbox lease to materialize "
            "run_id=%s project=%s timeout=%.2fs",
            run_id,
            self.project_id,
            wait_timeout_seconds,
        )
        while time.monotonic() < deadline:
            await asyncio.sleep(SHADOW_CLONE_LEASE_WAIT_POLL_SECONDS)
            lease = await get_run_sandbox_lease(run_id)
            if lease is not None:
                return lease
        return None

    async def _ensure_shadow_clone_bound_sandbox(self, client) -> Any:
        from sandbox.api import (
            attach_shadow_clone_bound_sandbox,
            ensure_shadow_clone_attachable_lease,
        )

        run_id = self._resolve_strict_shadow_clone_run_id()
        if not run_id:
            raise RuntimeError(
                f"Shadow Clone strict sandbox mode requires a bound run_id for project {self.project_id}"
            )

        lease = await self._await_shadow_clone_bound_lease(run_id)
        ensure_shadow_clone_attachable_lease(
            lease,
            run_id=run_id,
            project_id=self.project_id,
            sandbox_id=str(
                (lease or {}).get("sandbox_id") or self._get_runtime_sandbox_id() or ""
            ).strip(),
        )

        sandbox_obj = await attach_shadow_clone_bound_sandbox(client, lease=lease)
        refreshed_lease = await self._await_shadow_clone_bound_lease(run_id) or lease
        ensure_shadow_clone_attachable_lease(
            refreshed_lease,
            run_id=run_id,
            project_id=self.project_id,
            sandbox_id=str(
                (refreshed_lease or {}).get("sandbox_id")
                or self._extract_sandbox_id(sandbox_obj, self._get_runtime_sandbox_id())
                or ""
            ).strip(),
        )
        sandbox_info = self._parse_sandbox_info(refreshed_lease.get("sandbox_info"))

        self._bind_runtime_state(
            sandbox=sandbox_obj,
            sandbox_id=str(
                refreshed_lease.get("sandbox_id")
                or self._extract_sandbox_id(sandbox_obj, self._get_runtime_sandbox_id())
            ).strip(),
            sandbox_pass=str(
                sandbox_info.get("pass")
                or refreshed_lease.get("pass")
                or self._get_runtime_sandbox_pass()
                or ""
            ),
            sandbox_type=str(
                refreshed_lease.get("sandbox_type")
                or sandbox_info.get("type")
                or self.sandbox_type
                or "desktop"
            ).strip()
            or "desktop",
        )
        await self._ensure_claude_skills_runtime_ready(lease=refreshed_lease)
        return sandbox_obj

    async def _ensure_workspace_path(self) -> None:
        if self.sandbox_type not in ("code", "base"):
            return
        try:
            sandbox_obj = self._get_runtime_sandbox()
            if (
                sandbox_obj is not None
                and hasattr(sandbox_obj, "files")
                and hasattr(sandbox_obj.files, "make_dir")
            ):
                await self._run_blocking_sandbox_call(
                    "files.make_dir",
                    lambda: sandbox_obj.files.make_dir(self.workspace_path),
                    timeout_seconds=5.0,
                )
        except Exception as ensure_err:
            logger.debug(f"Workspace path ensure failed: {ensure_err}")

    async def _ensure_sandbox(self) -> Any:
        """Ensure a valid sandbox instance, retrieve it from the project if needed."""
        if self._get_runtime_sandbox() is not None:
            if self._strict_sandbox_mode:
                from sandbox.api import (
                    ensure_shadow_clone_attachable_lease,
                    is_shadow_clone_lease_attachable_binding_state,
                )

                run_id = self._resolve_strict_shadow_clone_run_id()
                # Reuse the same lease-wait path as first attach so planning-time
                # strict tools do not fail immediately on a transient pre-bind gap.
                lease = (
                    await self._await_shadow_clone_bound_lease(run_id)
                    if run_id
                    else None
                )
                lease_sandbox_id = str((lease or {}).get("sandbox_id") or "").strip()
                lease_binding_state = str(
                    (lease or {}).get("binding_state") or ""
                ).strip()
                runtime_sandbox_id = str(self._get_runtime_sandbox_id() or "").strip()
                if lease is None:
                    logger.info(
                        "[ShadowClone] Clearing cached strict sandbox handle because the lease is missing "
                        "run_id=%s project=%s cached_sandbox_id=%s",
                        run_id,
                        self.project_id,
                        runtime_sandbox_id or "<missing>",
                    )
                    self._clear_runtime_sandbox()
                    ensure_shadow_clone_attachable_lease(
                        lease,
                        run_id=run_id or "<missing>",
                        project_id=self.project_id,
                        sandbox_id=runtime_sandbox_id,
                    )
                elif not is_shadow_clone_lease_attachable_binding_state(
                    lease_binding_state
                ):
                    logger.info(
                        "[ShadowClone] Clearing cached strict sandbox handle due to non-attachable lease "
                        "run_id=%s project=%s cached_sandbox_id=%s lease_sandbox_id=%s binding_state=%s",
                        run_id,
                        self.project_id,
                        runtime_sandbox_id or "<missing>",
                        lease_sandbox_id or "<missing>",
                        lease_binding_state or "<missing>",
                    )
                    self._clear_runtime_sandbox()
                    ensure_shadow_clone_attachable_lease(
                        lease,
                        run_id=run_id or "<missing>",
                        project_id=self.project_id,
                        sandbox_id=lease_sandbox_id or runtime_sandbox_id,
                    )
                elif (
                    lease_sandbox_id
                    and runtime_sandbox_id
                    and lease_sandbox_id != runtime_sandbox_id
                ):
                    logger.info(
                        "[ShadowClone] Clearing cached strict sandbox handle due to lease drift run_id=%s "
                        "project=%s cached_sandbox_id=%s lease_sandbox_id=%s",
                        run_id,
                        self.project_id,
                        runtime_sandbox_id or "<missing>",
                        lease_sandbox_id or "<missing>",
                    )
                    self._clear_runtime_sandbox()
                else:
                    await self._ensure_claude_skills_runtime_ready(lease=lease)
            if (
                self._shared_shadow_clone_session is not None
                and self._shared_shadow_clone_session.sandbox_type
            ):
                self.sandbox_type = self._shared_shadow_clone_session.sandbox_type
            if self._get_runtime_sandbox() is not None:
                await self._ensure_workspace_path()
                return self._get_runtime_sandbox()

        async with self._ensure_lock:
            if self._get_runtime_sandbox() is None:
                if self.thread_manager is None:
                    raise RuntimeError("Sandbox tools require a thread manager")

                if self._strict_sandbox_mode:
                    client = await self.thread_manager.db.client
                    await self._ensure_shadow_clone_bound_sandbox(client)
                else:
                    redis_client = None
                    sandbox_create_lock_key = None
                    sandbox_create_lock_owner = None
                    try:
                        client = await self.thread_manager.db.client
                        sandbox_info = await self._load_project_sandbox_info(client)

                        if not sandbox_info.get("id"):
                            try:
                                redis_client = await redis.get_client()
                            except Exception as lock_error:
                                logger.warning(
                                    "Sandbox creation lock unavailable for project %s, proceeding unlocked: %s",
                                    self.project_id,
                                    lock_error,
                                )
                            else:
                                (
                                    sandbox_info,
                                    sandbox_create_lock_key,
                                    sandbox_create_lock_owner,
                                ) = await self._wait_for_sandbox_or_acquire_creation_lock(
                                    client,
                                    redis_client,
                                )

                        if not sandbox_info.get("id"):
                            sandbox_pass = str(uuid.uuid4())
                            try:
                                sandbox_obj = await self._create_sandbox_with_capacity(
                                    sandbox_pass=sandbox_pass,
                                    sandbox_type=self.sandbox_type,
                                )
                                sandbox_id = sandbox_obj.sandbox_id
                            except Exception as create_error:
                                logger.error(f"sandbox create error: {create_error}")
                                logger.error(
                                    f"sandbox create error type: {type(create_error)}"
                                )
                                raise

                            try:
                                vnc_url = None
                                if self.sandbox_type == "desktop":
                                    try:
                                        await self._run_blocking_sandbox_call(
                                            "stream.start",
                                            lambda: sandbox_obj.stream.start(),
                                        )
                                    except Exception as start_error:
                                        if (
                                            "already running"
                                            in str(start_error).lower()
                                        ):
                                            logger.info(
                                                "desktop stream already running, skip start step"
                                            )
                                        else:
                                            logger.error(
                                                f"desktop stream start error: {start_error}"
                                            )
                                            raise start_error

                                    vnc_url_raw = await self._run_blocking_sandbox_call(
                                        "stream.get_url",
                                        lambda: sandbox_obj.stream.get_url(),
                                    )
                                else:
                                    vnc_url_raw = None

                                if isinstance(vnc_url_raw, list):
                                    vnc_url = vnc_url_raw[0] if vnc_url_raw else ""
                                elif hasattr(vnc_url_raw, "url"):
                                    vnc_url = vnc_url_raw.url
                                else:
                                    vnc_url = str(vnc_url_raw) if vnc_url_raw else ""

                                website_url = vnc_url
                                token = None
                            except Exception:
                                import traceback

                                logger.error(
                                    f"desktop stream get url error: {traceback.format_exc()}"
                                )
                                vnc_url = None
                                website_url = None
                                token = None

                            try:
                                safe_vnc_url = None
                                if vnc_url:
                                    if isinstance(vnc_url, str):
                                        safe_vnc_url = vnc_url
                                    elif isinstance(vnc_url, list):
                                        safe_vnc_url = vnc_url[0] if vnc_url else ""
                                    else:
                                        safe_vnc_url = str(vnc_url)

                                safe_website_url = None
                                if website_url:
                                    if isinstance(website_url, str):
                                        safe_website_url = website_url
                                    elif isinstance(website_url, list):
                                        safe_website_url = (
                                            website_url[0] if website_url else ""
                                        )
                                    else:
                                        safe_website_url = str(website_url)

                                sandbox_metadata = {
                                    "id": str(sandbox_id),
                                    "pass": str(sandbox_pass),
                                    "type": str(self.sandbox_type),
                                    "vnc_preview": safe_vnc_url,
                                    "sandbox_url": safe_website_url,
                                    "token": str(token) if token else None,
                                    "mode": (
                                        "desktop_realtime"
                                        if self.sandbox_type == "desktop"
                                        else None
                                    ),
                                    "state": "running",
                                    "created_at": datetime.now(
                                        timezone.utc
                                    ).isoformat(),
                                }
                                sandbox_metadata = self._merge_template_lineage(
                                    sandbox_metadata,
                                    sandbox_type=self.sandbox_type,
                                    sandbox_obj=sandbox_obj,
                                    allow_expected_fallback=True,
                                )
                            except Exception as metadata_error:
                                logger.error(
                                    f"sandbox metadata build error: {metadata_error}"
                                )
                                logger.error(
                                    f"sandbox metadata build error type: {type(metadata_error)}"
                                )
                                raise Exception(
                                    f"sandbox metadata build error: {metadata_error}"
                                )

                            update_data = {"sandbox": json.dumps(sandbox_metadata)}

                            try:
                                project_table = client.table("projects").eq(
                                    "project_id", self.project_id
                                )
                                update_result = await project_table.update(update_data)
                            except Exception:
                                import traceback

                                logger.error(
                                    f"database update error: {traceback.format_exc()}"
                                )
                                raise

                            if update_result.data is None:
                                try:
                                    await delete_sandbox(sandbox_id)
                                except Exception:
                                    logger.error(
                                        f"Failed to delete sandbox {sandbox_id} after DB update failure",
                                        exc_info=True,
                                    )
                                raise Exception(
                                    "Database update failed when storing sandbox metadata"
                                )
                            if (
                                isinstance(update_result.data, list)
                                and len(update_result.data) == 0
                            ):
                                try:
                                    await client.table("projects").select(
                                        "project_id, name"
                                    ).eq(
                                        "project_id",
                                        self.project_id,
                                    ).execute()
                                except Exception as check_error:
                                    logger.error(f"project check error: {check_error}")

                                try:
                                    await delete_sandbox(sandbox_id)
                                except Exception:
                                    logger.error(
                                        f"Failed to delete sandbox {sandbox_id} after project not found",
                                        exc_info=True,
                                    )
                                raise Exception(
                                    f"Project {self.project_id} not found for sandbox metadata update"
                                )

                            logger.info(
                                f"database update success, updated {len(update_result.data)} rows"
                            )
                            self._bind_runtime_state(
                                sandbox=sandbox_obj,
                                sandbox_id=sandbox_id,
                                sandbox_pass=sandbox_pass,
                                sandbox_type=self.sandbox_type,
                            )
                            if WORKSPACE_ARTIFACTS_REHYDRATE_ON_RECOVERY and sandbox_id:
                                await self._rehydrate_workspace_artifacts(
                                    client,
                                    sandbox_id=sandbox_id,
                                )
                        else:
                            self._bind_runtime_state(
                                sandbox_id=sandbox_info["id"],
                                sandbox_pass=sandbox_info.get("pass"),
                                sandbox_type=sandbox_info.get("type")
                                or self.sandbox_type,
                            )
                            existing_sandbox_type = str(
                                sandbox_info.get("type", "desktop") or "desktop"
                            )
                            requested_sandbox_type = str(self.sandbox_type or "desktop")
                            migrated_from_type = None
                            force_recreate = False
                            force_recreate_reason = None
                            if (
                                requested_sandbox_type == "desktop"
                                and existing_sandbox_type != "desktop"
                            ):
                                force_recreate = True
                                force_recreate_reason = "sandbox_type_mismatch"
                                migrated_from_type = existing_sandbox_type
                                logger.info(
                                    "Sandbox type mismatch for project %s: existing=%s requested=%s. "
                                    "Auto-migrating to desktop.",
                                    self.project_id,
                                    existing_sandbox_type,
                                    requested_sandbox_type,
                                )
                            elif existing_sandbox_type != requested_sandbox_type:
                                logger.info(
                                    "Sandbox type mismatch for project %s: existing=%s requested=%s. "
                                    "Reusing existing type to avoid churn.",
                                    self.project_id,
                                    existing_sandbox_type,
                                    requested_sandbox_type,
                                )
                            resolved_sandbox_type = (
                                requested_sandbox_type
                                if force_recreate
                                else existing_sandbox_type
                            )
                            if not force_recreate:
                                stored_template_drift = self._get_template_drift(
                                    sandbox_info,
                                    sandbox_type=resolved_sandbox_type,
                                )
                                if stored_template_drift is not None:
                                    force_recreate = True
                                    force_recreate_reason = "sandbox_template_drift"
                                    logger.info(
                                        "Sandbox template drift detected for project %s: type=%s actual=%s expected=%s. "
                                        "Recreating project sandbox before reuse.",
                                        self.project_id,
                                        stored_template_drift["template_type"],
                                        stored_template_drift["actual_template_id"],
                                        stored_template_drift["expected_template_id"],
                                    )

                            try:
                                if force_recreate:
                                    raise RuntimeError(
                                        force_recreate_reason
                                        or "sandbox_recreate_required"
                                    )

                                if sandbox_info.get("state") == "paused":
                                    recovery_password = (
                                        self._get_runtime_sandbox_pass()
                                        or str(uuid.uuid4())
                                    )
                                    sandbox_obj, action = await asyncio.wait_for(
                                        resume_or_create_sandbox(
                                            password=recovery_password,
                                            project_id=self.project_id,
                                            sandbox_type=resolved_sandbox_type,
                                            sandbox_info=sandbox_info,
                                        ),
                                        timeout=SANDBOX_CONNECT_TIMEOUT_SECONDS,
                                    )
                                    runtime_template_drift = self._get_template_drift(
                                        sandbox_info,
                                        sandbox_type=resolved_sandbox_type,
                                        sandbox_obj=sandbox_obj,
                                    )
                                    if runtime_template_drift is not None:
                                        force_recreate = True
                                        force_recreate_reason = "sandbox_template_drift"
                                        raise RuntimeError(
                                            "sandbox_template_drift:"
                                            f"{runtime_template_drift['actual_template_id']}->"
                                            f"{runtime_template_drift['expected_template_id']}"
                                        )
                                    await self._finalize_recovered_sandbox(
                                        client,
                                        sandbox_info,
                                        sandbox_obj,
                                        sandbox_type=resolved_sandbox_type,
                                        action=action,
                                        password=recovery_password,
                                        old_sandbox_id=self._get_runtime_sandbox_id(),
                                        migrated_from_type=migrated_from_type,
                                    )
                                    logger.info(
                                        "Sandbox %s for project %s: %s",
                                        action,
                                        self.project_id,
                                        self._get_runtime_sandbox_id(),
                                    )
                                else:
                                    sandbox_obj = (
                                        await self._connect_sandbox_with_retry(
                                            self._get_runtime_sandbox_id(),
                                            resolved_sandbox_type,
                                        )
                                    )
                                    runtime_template_drift = self._get_template_drift(
                                        sandbox_info,
                                        sandbox_type=resolved_sandbox_type,
                                        sandbox_obj=sandbox_obj,
                                    )
                                    if runtime_template_drift is not None:
                                        force_recreate = True
                                        force_recreate_reason = "sandbox_template_drift"
                                        raise RuntimeError(
                                            "sandbox_template_drift:"
                                            f"{runtime_template_drift['actual_template_id']}->"
                                            f"{runtime_template_drift['expected_template_id']}"
                                        )
                                    self._set_runtime_sandbox(sandbox_obj)
                                    lineage_updates = self._merge_template_lineage(
                                        sandbox_info,
                                        sandbox_type=resolved_sandbox_type,
                                        sandbox_obj=sandbox_obj,
                                        fallback_template_id=self._normalize_template_value(
                                            sandbox_info.get("template_id")
                                        ),
                                        allow_expected_fallback=False,
                                    )
                                    changed_lineage = {
                                        key: value
                                        for key, value in lineage_updates.items()
                                        if sandbox_info.get(key) != value
                                        and value not in (None, "")
                                    }
                                    if changed_lineage:
                                        await self._update_sandbox_state(
                                            sandbox_info, **changed_lineage
                                        )
                            except Exception as connect_error:
                                recovery_password = (
                                    str(uuid.uuid4())
                                    if force_recreate
                                    else (
                                        self._get_runtime_sandbox_pass()
                                        or str(uuid.uuid4())
                                    )
                                )
                                recovery_action = "created"
                                recovery_sandbox_type = resolved_sandbox_type
                                if force_recreate:
                                    logger.info(
                                        "Recreating sandbox for project %s reason=%s existing_type=%s target_type=%s",
                                        self.project_id,
                                        force_recreate_reason
                                        or "sandbox_recreate_required",
                                        existing_sandbox_type,
                                        recovery_sandbox_type,
                                    )
                                else:
                                    logger.warning(
                                        "Sandbox %s unavailable: %s",
                                        self._get_runtime_sandbox_id(),
                                        connect_error,
                                    )
                                    logger.info(
                                        "Attempting sticky sandbox recovery for project %s using existing %s "
                                        "sandbox before recreate.",
                                        self.project_id,
                                        recovery_sandbox_type,
                                    )

                                old_sandbox_id = self._get_runtime_sandbox_id()
                                try:
                                    if force_recreate:
                                        sandbox_obj = (
                                            await self._create_sandbox_with_capacity(
                                                sandbox_pass=recovery_password,
                                                sandbox_type=recovery_sandbox_type,
                                            )
                                        )
                                    else:
                                        sandbox_obj, recovery_action = (
                                            await asyncio.wait_for(
                                                resume_or_create_sandbox(
                                                    password=recovery_password,
                                                    project_id=self.project_id,
                                                    sandbox_type=recovery_sandbox_type,
                                                    sandbox_info=sandbox_info,
                                                ),
                                                timeout=SANDBOX_CONNECT_TIMEOUT_SECONDS,
                                            )
                                        )
                                except Exception as recreate_error:
                                    logger.error(
                                        f"Failed to recover sandbox: {recreate_error}"
                                    )
                                    raise Exception(
                                        f"Sandbox {self._get_runtime_sandbox_id()} expired and recovery failed: "
                                        f"{recreate_error}"
                                    )

                                await self._finalize_recovered_sandbox(
                                    client,
                                    sandbox_info,
                                    sandbox_obj,
                                    sandbox_type=recovery_sandbox_type,
                                    action=recovery_action,
                                    password=recovery_password,
                                    old_sandbox_id=old_sandbox_id,
                                    migrated_from_type=migrated_from_type,
                                )
                                logger.info(
                                    f"Sandbox {recovery_action} for project {self.project_id}: "
                                    f"{self._get_runtime_sandbox_id()}"
                                )

                        if (
                            sandbox_create_lock_owner
                            and redis_client is not None
                            and sandbox_create_lock_key
                        ):
                            await self._release_sandbox_creation_lock(
                                redis_client,
                                sandbox_create_lock_key,
                                sandbox_create_lock_owner,
                            )
                    except Exception as e:
                        if (
                            sandbox_create_lock_owner
                            and redis_client is not None
                            and sandbox_create_lock_key
                        ):
                            await self._release_sandbox_creation_lock(
                                redis_client,
                                sandbox_create_lock_key,
                                sandbox_create_lock_owner,
                            )
                        logger.error(
                            f"Error retrieving/creating sandbox for project {self.project_id}: {str(e)}",
                            exc_info=True,
                        )
                        raise e

            if (
                self._shared_shadow_clone_session is not None
                and self._shared_shadow_clone_session.sandbox_type
            ):
                self.sandbox_type = self._shared_shadow_clone_session.sandbox_type
        await self._ensure_workspace_path()
        return self._get_runtime_sandbox()

    @property
    def sandbox(self) -> Any:
        """Get the sandbox instance, ensuring it exists."""
        sandbox_obj = self._get_runtime_sandbox()
        if sandbox_obj is None:
            raise RuntimeError("Sandbox not initialized. Call _ensure_sandbox() first.")
        return sandbox_obj

    @sandbox.setter
    def sandbox(self, value: Any) -> None:
        self._set_runtime_sandbox(value)

    @property
    def sandbox_id(self) -> str:
        """Get the sandbox ID, ensuring it exists."""
        sandbox_id = self._get_runtime_sandbox_id()
        if sandbox_id is None:
            raise RuntimeError(
                "Sandbox ID not initialized. Call _ensure_sandbox() first."
            )
        return sandbox_id

    def clean_path(self, path: str) -> str:
        """Clean and normalize a path to be relative to /workspace."""
        cleaned_path = clean_path(path, self.workspace_path)
        logger.debug(f"Cleaned path: {path} -> {cleaned_path}")
        return cleaned_path
