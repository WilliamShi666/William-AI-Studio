import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime, timezone, timedelta
import inspect
import json
import math
import os
import shlex
import threading
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar
import urllib.parse
import zipfile

from agentscope_integration.shadow_clone.sandbox_lease import (
    get_preferred_project_sandbox_lease,
    get_run_sandbox_lease,
    save_run_sandbox_lease,
    update_run_sandbox_lease,
    set_run_sandbox_binding_state,
)
from agentscope_integration.shadow_clone.constants import (
    is_shadow_clone_proactive_standby_enabled,
)
from utils.simple_auth_middleware import get_current_user_id_from_jwt, get_user_id_from_stream_auth, verify_thread_access
from fastapi import BackgroundTasks, FastAPI, UploadFile, File, HTTPException, APIRouter, Form, Depends, Request, Body
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
# from daytona_sdk import AsyncSandbox

from sandbox.sandbox import (
    extract_sandbox_template_id,
    get_or_start_sandbox,
    delete_sandbox,
    create_sandbox,
    pause_sandbox,
    resolve_sandbox_template_lineage,
    resume_or_create_sandbox,
    SandboxOriginalReuseExhausted,
    SandboxProviderFailure,
    clone_sandbox,
    extend_sandbox_timeout,
)
from sandbox.file_delivery_context import resolve_file_delivery_context
from utils.config import config
from utils.logger import logger
# from utils.auth_utils import get_optional_user_id
from services.postgresql import DBConnection
from services.workspace_artifacts import (
    guess_workspace_artifact_content_type,
    is_hidden_workspace_artifact_path,
    is_user_visible_workspace_artifact_path,
    workspace_artifacts,
)
from sandbox.renewal import renew_expiring_sandboxes
from services import redis, sandbox_capacity

# Initialize shared resources
router = APIRouter(tags=["sandbox"])
db = None

# User-facing workspace visibility is centralized in services.workspace_artifacts.
SANDBOX_ID_MAP_PREFIX = "sandbox:idmap:"
SANDBOX_ID_MAP_TTL = int(os.getenv("SANDBOX_ID_MAP_TTL", "3600"))
CLIENT_OPERATION_ID_HEADER = "X-Client-Operation-Id"

T = TypeVar("T")


def _safe_positive_float(value: Any, default: float, *, min_value: float = 0.1) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else min_value


def _safe_positive_int(value: Any, default: int, *, min_value: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else min_value


SANDBOX_ID_MAP_MAX_HOPS = _safe_positive_int(
    getattr(config, "SANDBOX_ID_MAP_MAX_HOPS", os.getenv("SANDBOX_ID_MAP_MAX_HOPS", 8)),
    8,
    min_value=1,
)


SANDBOX_IO_TIMEOUT_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_IO_TIMEOUT_SECONDS", 20.0),
    20.0,
)
SANDBOX_CONNECT_TIMEOUT_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_CONNECT_TIMEOUT_SECONDS", 20.0),
    20.0,
)
SANDBOX_CONNECT_RETRY_COUNT = _safe_positive_int(
    getattr(config, "SANDBOX_CONNECT_RETRY_COUNT", 0),
    0,
    min_value=0,
)
SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS", 0.4),
    0.4,
    min_value=0.0,
)
SANDBOX_RECOVERY_MAX_ATTEMPTS = _safe_positive_int(
    getattr(config, "SANDBOX_RECOVERY_MAX_ATTEMPTS", 1),
    1,
    min_value=0,
)
SANDBOX_IO_QUEUE_TIMEOUT_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_IO_QUEUE_TIMEOUT_SECONDS", 3.0),
    3.0,
)
SANDBOX_IO_MAX_CONCURRENT_GLOBAL = _safe_positive_int(
    getattr(config, "SANDBOX_IO_MAX_CONCURRENT_GLOBAL", 24),
    24,
)
SANDBOX_IO_MAX_CONCURRENT_PER_SANDBOX = _safe_positive_int(
    getattr(config, "SANDBOX_IO_MAX_CONCURRENT_PER_SANDBOX", 10),
    10,
)
SANDBOX_ARCHIVE_DOWNLOAD_ENABLED = bool(
    getattr(config, "SANDBOX_ARCHIVE_DOWNLOAD_ENABLED", True)
)
SANDBOX_AUTO_PAUSE_DELAY_ENABLED = bool(
    getattr(config, "SANDBOX_AUTO_PAUSE_DELAY_ENABLED", True)
)
SANDBOX_AUTO_PAUSE_GRACE_SECONDS = _safe_positive_int(
    getattr(config, "SANDBOX_AUTO_PAUSE_GRACE_SECONDS", 300),
    300,
    min_value=0,
)
SANDBOX_REATTACH_BUDGET_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_REATTACH_BUDGET_SECONDS", 5.0),
    5.0,
    min_value=0.0,
)
SANDBOX_PRECLONE_ENABLED = bool(
    getattr(config, "SANDBOX_PRECLONE_ENABLED", True)
)
SANDBOX_PRECLONE_THRESHOLD_SECONDS = _safe_positive_int(
    getattr(config, "SANDBOX_PRECLONE_THRESHOLD_SECONDS", 3000),
    3000,
    min_value=60,
)
SANDBOX_PRECLONE_RETRY_SECONDS = _safe_positive_int(
    getattr(config, "SANDBOX_PRECLONE_RETRY_SECONDS", 60),
    60,
    min_value=10,
)
SANDBOX_PRECLONE_GRACE_CLEANUP_SECONDS = _safe_positive_int(
    getattr(config, "SANDBOX_PRECLONE_GRACE_CLEANUP_SECONDS", 600),
    600,
    min_value=0,
)
SANDBOX_PRECLONE_CLONE_TIMEOUT_SECONDS = _safe_positive_int(
    getattr(config, "SANDBOX_PRECLONE_CLONE_TIMEOUT_SECONDS", 3600),
    3600,
    min_value=60,
)
SANDBOX_RECOVERY_LOCK_PREFIX = "sandbox:recover:lock:"
SANDBOX_RECOVERY_LOCK_TTL_SECONDS = _safe_positive_int(
    getattr(config, "SANDBOX_RECOVERY_LOCK_TTL_SECONDS", 15),
    15,
    min_value=5,
)
SANDBOX_RECOVERY_LOCK_WAIT_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_RECOVERY_LOCK_WAIT_SECONDS", 8.0),
    8.0,
    min_value=0.1,
)
SANDBOX_RECOVERY_LOCK_POLL_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_RECOVERY_LOCK_POLL_SECONDS", 0.2),
    0.2,
    min_value=0.05,
)
PROJECT_ACTIVE_RUN_KEY_PREFIX = str(
    getattr(config, "PROJECT_ACTIVE_RUN_KEY_PREFIX", "project_active_run:")
    or "project_active_run:"
)
SANDBOX_AUTO_PAUSE_ACTIVE_RUN_POLL_SECONDS = _safe_positive_float(
    getattr(config, "SANDBOX_AUTO_PAUSE_ACTIVE_RUN_POLL_SECONDS", 5.0),
    5.0,
    min_value=0.5,
)

_SEMAPHORE_ACQUIRE_POLL_SECONDS = 0.1
_sandbox_global_io_semaphore: Optional[threading.BoundedSemaphore] = None
_sandbox_io_semaphores: Dict[str, threading.BoundedSemaphore] = {}
_sandbox_io_semaphores_lock = threading.Lock()

_sandbox_connect_locks: Dict[str, asyncio.Lock] = {}
_sandbox_connect_locks_lock: Optional[asyncio.Lock] = None
_sandbox_project_recovery_locks: Dict[str, asyncio.Lock] = {}
_sandbox_project_recovery_locks_guard: Optional[asyncio.Lock] = None
_sandbox_auto_pause_tasks: Dict[str, asyncio.Task] = {}
_sandbox_auto_pause_tasks_lock: Optional[asyncio.Lock] = None
_sandbox_preclone_locks: Dict[str, asyncio.Lock] = {}
_sandbox_preclone_locks_lock: Optional[asyncio.Lock] = None
_sandbox_cleanup_tasks: Dict[str, asyncio.Task] = {}
_sandbox_cleanup_tasks_lock: Optional[asyncio.Lock] = None


class SandboxIOTimeoutError(Exception):
    def __init__(self, operation: str, timeout_seconds: float, sandbox_id: str, path: Optional[str] = None):
        self.operation = operation
        self.timeout_seconds = timeout_seconds
        self.sandbox_id = sandbox_id
        self.path = path
        super().__init__(
            f"Sandbox operation '{operation}' timed out after {timeout_seconds:.1f}s for sandbox {sandbox_id}"
        )


class SandboxIOQueueTimeoutError(Exception):
    def __init__(self, operation: str, queue_timeout_seconds: float, sandbox_id: str, path: Optional[str] = None):
        self.operation = operation
        self.queue_timeout_seconds = queue_timeout_seconds
        self.sandbox_id = sandbox_id
        self.path = path
        super().__init__(
            f"Sandbox operation '{operation}' queue wait timed out after {queue_timeout_seconds:.1f}s for sandbox {sandbox_id}"
        )


class SandboxConnectTimeoutError(Exception):
    def __init__(self, sandbox_id: str, sandbox_type: str, timeout_seconds: float):
        self.sandbox_id = sandbox_id
        self.sandbox_type = sandbox_type
        self.timeout_seconds = timeout_seconds
        super().__init__(
            f"Timed out while connecting sandbox {sandbox_id} ({sandbox_type}) after {timeout_seconds:.1f}s"
        )


class ShadowCloneStrictSandboxError(RuntimeError):
    def __init__(
        self,
        *,
        run_id: str,
        sandbox_id: str,
        project_id: str,
        detail: str,
        error_code: str,
        recoverable: bool,
        binding_state: Optional[str] = None,
        root_cause_code: Optional[str] = None,
        recovery_action: Optional[str] = None,
        provider_reason: Optional[str] = None,
        provider_status_code: Optional[int] = None,
        provider_code: Optional[int] = None,
        provider_retryable: Optional[bool] = None,
    ) -> None:
        self.run_id = run_id
        self.sandbox_id = sandbox_id
        self.project_id = project_id
        self.detail = detail
        self.error_code = error_code
        self.recoverable = recoverable
        self.binding_state = str(binding_state or "").strip().lower() or None
        self.root_cause_code = str(root_cause_code or "").strip() or None
        self.recovery_action = str(recovery_action or "").strip() or None
        self.provider_reason = str(provider_reason or "").strip() or None
        self.provider_status_code = provider_status_code
        self.provider_code = provider_code
        self.provider_retryable = provider_retryable
        super().__init__(detail)


_DEFAULT_SHADOW_CLONE_ATTACHABLE_BINDING_STATES = frozenset({"locked", "reattaching"})


def _normalize_shadow_clone_binding_state(value: Any) -> str:
    return str(value or "").strip().lower()


def is_shadow_clone_lease_attachable_binding_state(state: Any) -> bool:
    normalized = _normalize_shadow_clone_binding_state(state)
    if not normalized:
        return False

    try:
        from agentscope_integration.shadow_clone import sandbox_lease as sandbox_lease_module

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
                    _normalize_shadow_clone_binding_state(item)
                    for item in states
                    if _normalize_shadow_clone_binding_state(item)
                }
                if normalized_states:
                    return normalized in normalized_states
    except Exception:
        pass

    return normalized in _DEFAULT_SHADOW_CLONE_ATTACHABLE_BINDING_STATES


def _extract_shadow_clone_root_cause_metadata(error: Optional[BaseException]) -> Dict[str, Any]:
    candidate = error
    if isinstance(candidate, SandboxOriginalReuseExhausted):
        candidate = candidate.last_error or candidate

    if isinstance(candidate, ShadowCloneStrictSandboxError):
        return {
            "root_cause_code": candidate.root_cause_code,
            "provider_reason": candidate.provider_reason,
            "provider_status_code": candidate.provider_status_code,
            "provider_code": candidate.provider_code,
            "provider_retryable": candidate.provider_retryable,
            "binding_state": candidate.binding_state,
        }

    if isinstance(candidate, SandboxProviderFailure):
        return {
            "root_cause_code": (
                f"SHADOW_CLONE_{candidate.root_cause_code}"
                if candidate.root_cause_code and not candidate.root_cause_code.startswith("SHADOW_CLONE_")
                else candidate.root_cause_code
            ),
            "provider_reason": candidate.provider_reason,
            "provider_status_code": candidate.provider_http_status,
            "provider_code": candidate.provider_code,
            "provider_retryable": candidate.retryable,
        }

    return {}


def _build_shadow_clone_missing_lease_error(
    *,
    run_id: str,
    project_id: str,
    sandbox_id: str,
) -> ShadowCloneStrictSandboxError:
    return ShadowCloneStrictSandboxError(
        run_id=run_id,
        sandbox_id=sandbox_id or "<missing>",
        project_id=project_id,
        detail=(
            f"Shadow Clone sandbox lease missing for run {run_id} (project={project_id}). "
            "Strict tool attach is blocked until the run publishes a valid bound sandbox lease."
        ),
        error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
        recoverable=False,
        root_cause_code="SHADOW_CLONE_SANDBOX_LEASE_MISSING",
        recovery_action="replan",
    )


def _build_shadow_clone_project_mismatch_error(
    *,
    lease: Dict[str, Any],
    run_id: str,
    project_id: str,
    sandbox_id: str,
) -> ShadowCloneStrictSandboxError:
    lease_project_id = str(lease.get("project_id") or "").strip() or "<missing>"
    return ShadowCloneStrictSandboxError(
        run_id=run_id,
        sandbox_id=sandbox_id or "<missing>",
        project_id=project_id,
        detail=(
            f"Shadow Clone sandbox lease project mismatch for run {run_id}: "
            f"{lease_project_id} != {project_id}."
        ),
        error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
        recoverable=False,
        root_cause_code="SHADOW_CLONE_SANDBOX_LEASE_PROJECT_MISMATCH",
        recovery_action="replan",
    )


def _build_shadow_clone_non_attachable_lease_error(
    lease: Dict[str, Any],
    *,
    run_id: str,
    project_id: str,
    sandbox_id: str,
) -> ShadowCloneStrictSandboxError:
    binding_state = _normalize_shadow_clone_binding_state(lease.get("binding_state"))
    last_error = str(lease.get("last_error") or "").strip()
    is_clone_recovery_state = binding_state in {"recovery_required", "recovering"}
    if is_clone_recovery_state:
        detail = last_error or (
            f"Shadow Clone sandbox {sandbox_id} is in {binding_state} for run {run_id}. "
            "Strict tool attach is blocked until standby clone recovery completes."
        )
        return ShadowCloneStrictSandboxError(
            run_id=run_id,
            sandbox_id=sandbox_id,
            project_id=project_id,
            detail=detail,
            error_code="SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
            recoverable=True,
            binding_state=binding_state,
            root_cause_code="SHADOW_CLONE_SANDBOX_LEASE_NOT_ATTACHABLE",
            recovery_action="clone_recovery",
        )

    detail = last_error or (
        f"Shadow Clone sandbox {sandbox_id} is in non-attachable lease state {binding_state or '<missing>'} "
        f"for run {run_id}."
    )
    return ShadowCloneStrictSandboxError(
        run_id=run_id,
        sandbox_id=sandbox_id,
        project_id=project_id,
        detail=detail,
        error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
        recoverable=False,
        binding_state=binding_state or None,
        root_cause_code="SHADOW_CLONE_SANDBOX_LEASE_NOT_ATTACHABLE",
        recovery_action="replan",
    )


def ensure_shadow_clone_attachable_lease(
    lease: Optional[Dict[str, Any]],
    *,
    run_id: str,
    project_id: str,
    sandbox_id: str,
) -> Dict[str, Any]:
    if lease is None:
        raise _build_shadow_clone_missing_lease_error(
            run_id=run_id,
            project_id=project_id,
            sandbox_id=sandbox_id,
        )

    if str(lease.get("project_id") or "").strip() != project_id:
        raise _build_shadow_clone_project_mismatch_error(
            lease=lease,
            run_id=run_id,
            project_id=project_id,
            sandbox_id=sandbox_id,
        )

    binding_state = _normalize_shadow_clone_binding_state(lease.get("binding_state"))
    if binding_state and not is_shadow_clone_lease_attachable_binding_state(binding_state):
        raise _build_shadow_clone_non_attachable_lease_error(
            lease,
            run_id=run_id,
            project_id=project_id,
            sandbox_id=sandbox_id,
        )
    return lease


def _get_global_io_semaphore() -> threading.BoundedSemaphore:
    global _sandbox_global_io_semaphore
    with _sandbox_io_semaphores_lock:
        if _sandbox_global_io_semaphore is None:
            _sandbox_global_io_semaphore = threading.BoundedSemaphore(
                SANDBOX_IO_MAX_CONCURRENT_GLOBAL
            )
        return _sandbox_global_io_semaphore


def _get_io_semaphore_lock() -> threading.Lock:
    return _sandbox_io_semaphores_lock


def _get_connect_lock_guard() -> asyncio.Lock:
    global _sandbox_connect_locks_lock
    if _sandbox_connect_locks_lock is None:
        _sandbox_connect_locks_lock = asyncio.Lock()
    return _sandbox_connect_locks_lock


def _get_auto_pause_tasks_lock() -> asyncio.Lock:
    global _sandbox_auto_pause_tasks_lock
    if _sandbox_auto_pause_tasks_lock is None:
        _sandbox_auto_pause_tasks_lock = asyncio.Lock()
    return _sandbox_auto_pause_tasks_lock


def _get_project_recovery_lock_guard() -> asyncio.Lock:
    global _sandbox_project_recovery_locks_guard
    if _sandbox_project_recovery_locks_guard is None:
        _sandbox_project_recovery_locks_guard = asyncio.Lock()
    return _sandbox_project_recovery_locks_guard


def _get_preclone_lock_guard() -> asyncio.Lock:
    global _sandbox_preclone_locks_lock
    if _sandbox_preclone_locks_lock is None:
        _sandbox_preclone_locks_lock = asyncio.Lock()
    return _sandbox_preclone_locks_lock


async def _get_project_preclone_lock(project_id: str) -> asyncio.Lock:
    guard = _get_preclone_lock_guard()
    async with guard:
        lock = _sandbox_preclone_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _sandbox_preclone_locks[project_id] = lock
        return lock


def _get_cleanup_tasks_lock() -> asyncio.Lock:
    global _sandbox_cleanup_tasks_lock
    if _sandbox_cleanup_tasks_lock is None:
        _sandbox_cleanup_tasks_lock = asyncio.Lock()
    return _sandbox_cleanup_tasks_lock


def _get_per_sandbox_io_semaphore(sandbox_id: str) -> threading.BoundedSemaphore:
    lock = _get_io_semaphore_lock()
    with lock:
        semaphore = _sandbox_io_semaphores.get(sandbox_id)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(
                SANDBOX_IO_MAX_CONCURRENT_PER_SANDBOX
            )
            _sandbox_io_semaphores[sandbox_id] = semaphore
        return semaphore


async def _get_sandbox_connect_lock(sandbox_id: str) -> asyncio.Lock:
    guard = _get_connect_lock_guard()
    async with guard:
        lock = _sandbox_connect_locks.get(sandbox_id)
        if lock is None:
            lock = asyncio.Lock()
            _sandbox_connect_locks[sandbox_id] = lock
        return lock


async def _get_project_recovery_lock(project_id: str) -> asyncio.Lock:
    guard = _get_project_recovery_lock_guard()
    async with guard:
        lock = _sandbox_project_recovery_locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            _sandbox_project_recovery_locks[project_id] = lock
        return lock


async def _release_distributed_recovery_lock(lock_key: str, token: str) -> None:
    if not lock_key or not token:
        return
    try:
        redis_client = await redis.get_client()
        await redis_client.eval(
            """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""",
            1,
            lock_key,
            token,
        )
    except Exception as release_error:
        logger.warning(
            "Failed to release distributed sandbox recovery lock",
            extra={"lock_key": lock_key, "error": str(release_error)},
        )


@asynccontextmanager
async def _project_recovery_singleflight(project_id: str):
    """Serialize stale-sandbox recovery to avoid concurrent create races."""
    local_lock = await _get_project_recovery_lock(project_id)
    async with local_lock:
        lock_key = f"{SANDBOX_RECOVERY_LOCK_PREFIX}{project_id}"
        token = f"{time.time_ns()}:{project_id}"
        distributed_acquired = False
        distributed_supported = True
        deadline = time.monotonic() + SANDBOX_RECOVERY_LOCK_WAIT_SECONDS

        while distributed_supported and time.monotonic() <= deadline:
            try:
                distributed_acquired = bool(
                    await redis.set(
                        lock_key,
                        token,
                        ex=SANDBOX_RECOVERY_LOCK_TTL_SECONDS,
                        nx=True,
                    )
                )
            except Exception as lock_error:
                distributed_supported = False
                logger.warning(
                    "Distributed sandbox recovery lock unavailable; using local singleflight only",
                    extra={"project_id": project_id, "error": str(lock_error)},
                )
                break

            if distributed_acquired:
                logger.info(
                    "Sandbox recovery lock acquired",
                    extra={"project_id": project_id, "lock_key": lock_key},
                )
                break

            await asyncio.sleep(SANDBOX_RECOVERY_LOCK_POLL_SECONDS)

        if distributed_supported and not distributed_acquired:
            logger.warning(
                "Timed out waiting for distributed sandbox recovery lock; continuing with local lock",
                extra={"project_id": project_id, "lock_key": lock_key},
            )

        try:
            yield
        finally:
            if distributed_acquired:
                await _release_distributed_recovery_lock(lock_key, token)


async def _acquire_semaphore_with_timeout(
    semaphore: threading.BoundedSemaphore,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_seconds)

    while True:
        if semaphore.acquire(blocking=False):
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False

        await asyncio.sleep(min(_SEMAPHORE_ACQUIRE_POLL_SECONDS, remaining))


@asynccontextmanager
async def _sandbox_io_capacity_guard(
    sandbox_id: str,
    operation: str,
    *,
    path: Optional[str] = None,
    lease_ttl_seconds: Optional[int] = None,
):
    queue_timeout = SANDBOX_IO_QUEUE_TIMEOUT_SECONDS
    global_sem = _get_global_io_semaphore()
    per_sandbox_sem = _get_per_sandbox_io_semaphore(sandbox_id)
    async with AsyncExitStack() as exit_stack:
        use_process_local_per_sandbox_guard = (
            not sandbox_capacity.is_sandbox_per_sandbox_capacity_enabled()
        )
        if not use_process_local_per_sandbox_guard:
            try:
                await exit_stack.enter_async_context(
                    sandbox_capacity.acquire_sandbox_per_sandbox_capacity(
                        holder_kind="sandbox_io",
                        sandbox_id=sandbox_id,
                        operation=operation,
                        queue_timeout_seconds=queue_timeout,
                        lease_ttl_seconds=lease_ttl_seconds,
                        retain_lease_on_exceptions=(
                            SandboxIOTimeoutError,
                            asyncio.CancelledError,
                        ),
                    )
                )
            except sandbox_capacity.SandboxPerSandboxCapacityTimeoutError as capacity_timeout_error:
                raise SandboxIOQueueTimeoutError(
                    operation,
                    queue_timeout,
                    sandbox_id,
                    path=path,
                ) from capacity_timeout_error
            except sandbox_capacity.SandboxPerSandboxCapacityBackendUnavailableError as capacity_error:
                use_process_local_per_sandbox_guard = True
                logger.warning(
                    "Shared sandbox per-sandbox capacity unavailable during %s for sandbox %s; "
                    "falling back to process-local I/O limiter: %s",
                    operation,
                    sandbox_id,
                    capacity_error,
                )

        if use_process_local_per_sandbox_guard:
            per_sandbox_acquired = await _acquire_semaphore_with_timeout(per_sandbox_sem, queue_timeout)
            if not per_sandbox_acquired:
                raise SandboxIOQueueTimeoutError(operation, queue_timeout, sandbox_id, path=path)
            exit_stack.callback(per_sandbox_sem.release)

        use_process_local_global_guard = not sandbox_capacity.is_sandbox_global_capacity_enabled()

        if not use_process_local_global_guard:
            try:
                await exit_stack.enter_async_context(
                    sandbox_capacity.acquire_sandbox_global_capacity(
                        holder_kind="sandbox_io",
                        sandbox_id=sandbox_id,
                        operation=operation,
                        queue_timeout_seconds=queue_timeout,
                        lease_ttl_seconds=lease_ttl_seconds,
                        retain_lease_on_exceptions=(
                            SandboxIOTimeoutError,
                            asyncio.CancelledError,
                        ),
                    )
                )
            except sandbox_capacity.SandboxGlobalCapacityTimeoutError as capacity_timeout_error:
                raise SandboxIOQueueTimeoutError(
                    operation,
                    queue_timeout,
                    sandbox_id,
                    path=path,
                ) from capacity_timeout_error
            except sandbox_capacity.SandboxGlobalCapacityBackendUnavailableError as capacity_error:
                use_process_local_global_guard = True
                logger.warning(
                    "Shared sandbox global capacity unavailable during %s for sandbox %s; "
                    "falling back to process-local I/O limiter: %s",
                    operation,
                    sandbox_id,
                    capacity_error,
                )

        if use_process_local_global_guard:
            global_acquired = await _acquire_semaphore_with_timeout(global_sem, queue_timeout)
            if not global_acquired:
                raise SandboxIOQueueTimeoutError(operation, queue_timeout, sandbox_id, path=path)
            exit_stack.callback(global_sem.release)

        yield


async def _run_blocking_sandbox_call(
    operation: str,
    call: Callable[[], T],
    *,
    timeout_seconds: float,
    sandbox_id: str,
    path: Optional[str] = None,
) -> T:
    start_time = time.perf_counter()
    try:
        result = await asyncio.wait_for(asyncio.to_thread(call), timeout=timeout_seconds)
        elapsed_ms = (time.perf_counter() - start_time) * 1000
        logger.debug(
            "Sandbox operation finished",
            extra={
                "sandbox_id": sandbox_id,
                "operation": operation,
                "path": path,
                "elapsed_ms": round(elapsed_ms, 2),
            },
        )
        return result
    except asyncio.TimeoutError as timeout_error:
        logger.warning(
            f"Sandbox operation timeout: {operation} (sandbox={sandbox_id}, path={path}, timeout={timeout_seconds}s)"
        )
        raise SandboxIOTimeoutError(operation, timeout_seconds, sandbox_id, path=path) from timeout_error


async def _run_guarded_sandbox_io(
    sandbox_id: str,
    operation: str,
    call: Callable[[], T],
    *,
    timeout_seconds: Optional[float] = None,
    path: Optional[str] = None,
) -> T:
    effective_timeout = timeout_seconds or SANDBOX_IO_TIMEOUT_SECONDS
    async with _sandbox_io_capacity_guard(
        sandbox_id,
        operation,
        path=path,
        lease_ttl_seconds=sandbox_capacity.compute_sandbox_capacity_lease_ttl_seconds(
            operation_timeout_seconds=effective_timeout,
            queue_timeout_seconds=SANDBOX_IO_QUEUE_TIMEOUT_SECONDS,
        ),
    ):
        return await _run_blocking_sandbox_call(
            operation,
            call,
            timeout_seconds=effective_timeout,
            sandbox_id=sandbox_id,
            path=path,
        )


def _to_response_bytes(content: Any) -> bytes:
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if isinstance(content, str):
        return content.encode()
    return str(content).encode()


def _is_dir_entry(entry: Any) -> bool:
    if hasattr(entry, "is_dir"):
        return bool(getattr(entry, "is_dir"))
    if hasattr(entry, "isDir"):
        return bool(getattr(entry, "isDir"))
    if hasattr(entry, "type"):
        return getattr(entry, "type") == "directory"
    return False


def _entry_mod_time(entry: Any) -> str:
    if hasattr(entry, "mod_time"):
        return str(getattr(entry, "mod_time"))
    if hasattr(entry, "modTime"):
        return str(getattr(entry, "modTime"))
    if hasattr(entry, "modified"):
        return str(getattr(entry, "modified"))
    return ""


def _relative_workspace_path(path: str) -> str:
    if path == "/workspace":
        return "workspace"
    if path.startswith('/workspace/'):
        return path[len('/workspace/'):]
    return path.lstrip('/')


def _is_not_found_error(error: Exception) -> bool:
    if isinstance(error, FileNotFoundError):
        return True
    message = str(error).lower()
    return 'not found' in message or 'no such file' in message


def _is_not_directory_error(error: Exception) -> bool:
    if isinstance(error, NotADirectoryError):
        return True
    message = str(error).lower()
    return 'not a directory' in message


def _is_directory_error(error: Exception) -> bool:
    if isinstance(error, IsADirectoryError):
        return True
    message = str(error).lower()
    return 'is a directory' in message or 'illegal operation on a directory' in message


def _sandbox_error_to_http(error: Exception, *, action: str) -> HTTPException:
    if isinstance(error, SandboxIOQueueTimeoutError):
        retry_after = max(1, int(math.ceil(error.queue_timeout_seconds)))
        return HTTPException(
            status_code=429,
            detail={
                "message": f"Sandbox is busy while attempting to {action}. Please retry.",
                "error_code": "SANDBOX_IO_QUEUE_TIMEOUT",
                "recoverable": True,
                "retry_after_seconds": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    if isinstance(error, SandboxIOTimeoutError):
        return HTTPException(
            status_code=504,
            detail={
                "message": f"Timed out while attempting to {action} after {error.timeout_seconds:.1f}s.",
                "error_code": "SANDBOX_IO_TIMEOUT",
                "recoverable": True,
            },
        )

    if isinstance(error, SandboxConnectTimeoutError):
        return HTTPException(
            status_code=504,
            detail={
                "message": str(error),
                "error_code": "SANDBOX_CONNECT_TIMEOUT",
                "recoverable": True,
                "sandbox_id": error.sandbox_id,
                "sandbox_type": error.sandbox_type,
            },
        )

    return HTTPException(
        status_code=500,
        detail={
            "message": str(error),
            "error_code": "SANDBOX_INTERNAL_ERROR",
            "recoverable": False,
        },
    )


def _archive_error_payload(error: Exception) -> Dict[str, Any]:
    if isinstance(error, SandboxIOQueueTimeoutError):
        return {
            'type': 'queue_timeout',
            'error_code': 'SANDBOX_IO_QUEUE_TIMEOUT',
            'message': 'Sandbox queue timeout while waiting for IO slot',
            'retry_after_seconds': max(1, int(math.ceil(error.queue_timeout_seconds))),
        }
    if isinstance(error, SandboxIOTimeoutError):
        return {
            'type': 'timeout',
            'error_code': 'SANDBOX_IO_TIMEOUT',
            'message': f"Sandbox operation timed out after {error.timeout_seconds:.1f}s",
        }
    if isinstance(error, SandboxConnectTimeoutError):
        return {
            'type': 'connect_timeout',
            'error_code': 'SANDBOX_CONNECT_TIMEOUT',
            'message': str(error),
        }
    return {'type': type(error).__name__, 'error_code': 'SANDBOX_INTERNAL_ERROR', 'message': str(error)}


def _is_auto_pause_enabled() -> bool:
    raw_value = os.getenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE")
    if raw_value is None:
        return bool(getattr(config, "SANDBOX_AUTO_PAUSE_ON_COMPLETE", True))
    return raw_value.lower() == "true"


def _get_auto_pause_grace_seconds() -> int:
    raw_value = os.getenv("SANDBOX_AUTO_PAUSE_GRACE_SECONDS")
    if raw_value is None:
        return max(0, SANDBOX_AUTO_PAUSE_GRACE_SECONDS)
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return max(0, SANDBOX_AUTO_PAUSE_GRACE_SECONDS)


def _get_reattach_budget_seconds() -> float:
    raw_value = os.getenv("SANDBOX_REATTACH_BUDGET_SECONDS")
    if raw_value is None:
        return max(0.0, SANDBOX_REATTACH_BUDGET_SECONDS)
    try:
        return max(0.0, float(raw_value))
    except (TypeError, ValueError):
        return max(0.0, SANDBOX_REATTACH_BUDGET_SECONDS)


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _project_active_run_key(project_id: Optional[str]) -> str:
    if not project_id:
        return ""
    return f"{PROJECT_ACTIVE_RUN_KEY_PREFIX}{project_id}"


async def _project_has_active_run(project_id: Optional[str]) -> bool:
    key = _project_active_run_key(project_id)
    if not key:
        return False
    try:
        raw_value = await redis.get(key)
    except Exception:
        return False
    if not raw_value:
        return False
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else {}
    except Exception:
        return True
    return bool(str(parsed.get("agent_run_id") or "").strip())


def _is_preclone_enabled() -> bool:
    raw_value = os.getenv("SANDBOX_PRECLONE_ENABLED")
    if raw_value is None:
        return SANDBOX_PRECLONE_ENABLED
    return raw_value.lower() == "true"


def _get_preclone_threshold_seconds() -> int:
    raw_value = os.getenv("SANDBOX_PRECLONE_THRESHOLD_SECONDS")
    if raw_value is None:
        return SANDBOX_PRECLONE_THRESHOLD_SECONDS
    try:
        return max(60, int(raw_value))
    except (TypeError, ValueError):
        return SANDBOX_PRECLONE_THRESHOLD_SECONDS


def _get_preclone_retry_seconds() -> int:
    raw_value = os.getenv("SANDBOX_PRECLONE_RETRY_SECONDS")
    if raw_value is None:
        return SANDBOX_PRECLONE_RETRY_SECONDS
    try:
        return max(10, int(raw_value))
    except (TypeError, ValueError):
        return SANDBOX_PRECLONE_RETRY_SECONDS


def _get_preclone_cleanup_seconds() -> int:
    raw_value = os.getenv("SANDBOX_PRECLONE_GRACE_CLEANUP_SECONDS")
    if raw_value is None:
        return SANDBOX_PRECLONE_GRACE_CLEANUP_SECONDS
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return SANDBOX_PRECLONE_GRACE_CLEANUP_SECONDS


def _get_preclone_clone_timeout_seconds() -> int:
    raw_value = os.getenv("SANDBOX_PRECLONE_CLONE_TIMEOUT_SECONDS")
    if raw_value is None:
        return SANDBOX_PRECLONE_CLONE_TIMEOUT_SECONDS
    try:
        return max(60, int(raw_value))
    except (TypeError, ValueError):
        return SANDBOX_PRECLONE_CLONE_TIMEOUT_SECONDS


def _resolve_sandbox_lifecycle_start(sandbox_info: Dict[str, Any], now: datetime) -> datetime:
    lifecycle_started = _parse_iso_datetime(sandbox_info.get('lifecycle_started_at'))
    if lifecycle_started:
        return lifecycle_started

    created_at = _parse_iso_datetime(sandbox_info.get('created_at'))
    if created_at:
        return created_at

    return now


def _compute_preclone_due_at(reference: datetime, threshold_seconds: int) -> str:
    return (reference + timedelta(seconds=max(60, threshold_seconds))).isoformat()


def _get_sandbox_age_seconds(sandbox_info: Dict[str, Any], now: datetime) -> float:
    lifecycle_start = _resolve_sandbox_lifecycle_start(sandbox_info, now)
    return max(0.0, (now - lifecycle_start).total_seconds())


def _ensure_sandbox_lifecycle_fields(
    sandbox_info: Dict[str, Any],
    *,
    now: datetime,
    threshold_seconds: int,
) -> Dict[str, Any]:
    updated = {**sandbox_info}
    lifecycle_start = _resolve_sandbox_lifecycle_start(updated, now)
    updated['lifecycle_started_at'] = lifecycle_start.isoformat()
    if not updated.get('preclone_due_at'):
        updated['preclone_due_at'] = _compute_preclone_due_at(lifecycle_start, threshold_seconds)
    return updated


async def _schedule_old_sandbox_cleanup(old_sandbox_id: str) -> None:
    cleanup_seconds = _get_preclone_cleanup_seconds()
    if cleanup_seconds <= 0 or not old_sandbox_id:
        return

    lock = _get_cleanup_tasks_lock()
    async with lock:
        existing_task = _sandbox_cleanup_tasks.get(old_sandbox_id)
        if existing_task and not existing_task.done():
            return

        async def _cleanup() -> None:
            await asyncio.sleep(cleanup_seconds)
            try:
                await delete_sandbox(old_sandbox_id)
                logger.info(f"Cleaned up preclone source sandbox {old_sandbox_id}")
            except Exception as cleanup_error:
                logger.warning(f"Failed to clean preclone source sandbox {old_sandbox_id}: {cleanup_error}")

        task = asyncio.create_task(_cleanup(), name=f"sandbox-preclone-cleanup:{old_sandbox_id}")
        _sandbox_cleanup_tasks[old_sandbox_id] = task

        def _cleanup_task(done_task: asyncio.Task, *, sandbox_id: str = old_sandbox_id) -> None:
            current = _sandbox_cleanup_tasks.get(sandbox_id)
            if current is done_task:
                _sandbox_cleanup_tasks.pop(sandbox_id, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as task_error:
                logger.warning(f"Preclone cleanup task failed for sandbox {sandbox_id}: {task_error}")

        task.add_done_callback(_cleanup_task)


def _extract_sandbox_object_id(sandbox_obj: Any, fallback: str = "") -> str:
    resolved_id = (
        getattr(sandbox_obj, 'sandbox_id', None)
        or getattr(sandbox_obj, 'id', None)
        or fallback
    )
    return str(resolved_id or '').strip()


def _normalize_template_value(value: Any) -> Optional[str]:
    candidate = str(value or "").strip()
    return candidate or None


def _merge_sandbox_template_lineage(
    sandbox_info: Dict[str, Any],
    *,
    sandbox_type: str,
    sandbox_obj: Any = None,
    fallback_template_id: Optional[str] = None,
    allow_expected_fallback: bool = False,
) -> Dict[str, Any]:
    lineage = resolve_sandbox_template_lineage(sandbox_type)
    resolved_template_id = _normalize_template_value(
        extract_sandbox_template_id(sandbox_obj)
        or fallback_template_id
        or (lineage.get("template_id") if allow_expected_fallback else None)
    )
    merged = dict(sandbox_info)
    merged["template_type"] = str(lineage.get("template_type") or sandbox_type or "desktop")
    merged["template_source"] = str(lineage.get("template_source") or "config_env")
    if resolved_template_id:
        merged["template_id"] = resolved_template_id
    return merged


def _build_shadow_clone_bound_sandbox_info(lease: Dict[str, Any]) -> Dict[str, Any]:
    sandbox_id = str(lease.get("sandbox_id") or "").strip()
    sandbox_type = str(lease.get("sandbox_type") or "desktop").strip() or "desktop"
    sandbox_info = _parse_sandbox_info(lease.get("sandbox_info"))
    return {
        **sandbox_info,
        "id": sandbox_id,
        "type": sandbox_type,
    }


async def _clear_disabled_shadow_clone_standby_metadata(
    run_id: str,
    lease: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    current_lease = dict(lease or {})
    if is_shadow_clone_proactive_standby_enabled():
        return current_lease
    if not str(current_lease.get("standby_sandbox_id") or "").strip():
        return current_lease

    cleared_lease = await update_run_sandbox_lease(
        run_id,
        standby_sandbox_id=None,
        standby_sandbox_type=None,
        standby_sandbox_info={},
        standby_snapshot_template_id=None,
        standby_source_sandbox_id=None,
        standby_created_at=None,
        last_clone_error=None,
    )
    return dict(cleared_lease or current_lease)


def _shadow_clone_strict_error_to_http(error: ShadowCloneStrictSandboxError) -> HTTPException:
    detail = {
        "message": error.detail,
        "error_code": error.error_code,
        "recoverable": error.recoverable,
        "sandbox_id": error.sandbox_id,
        "agent_run_id": error.run_id,
        "project_id": error.project_id,
    }
    if error.binding_state:
        detail["binding_state"] = error.binding_state
    if error.root_cause_code:
        detail["root_cause_code"] = error.root_cause_code
    if error.recovery_action:
        detail["recovery_action"] = error.recovery_action
    if error.provider_reason:
        detail["provider_reason"] = error.provider_reason
    if error.provider_status_code is not None:
        detail["provider_status_code"] = error.provider_status_code
    if error.provider_code is not None:
        detail["provider_code"] = error.provider_code
    if error.provider_retryable is not None:
        detail["provider_retryable"] = error.provider_retryable
    status_code = 503 if error.recoverable else 409
    return HTTPException(
        status_code=status_code,
        detail=detail,
    )


def _extract_stream_url(raw_value: Any) -> str:
    if isinstance(raw_value, list):
        candidate = raw_value[0] if raw_value else ''
    elif hasattr(raw_value, 'url'):
        candidate = getattr(raw_value, 'url')
    else:
        candidate = raw_value
    return str(candidate or '')


async def _probe_sandbox_is_running(sandbox_obj: Any, *, sandbox_id: str) -> bool:
    is_running_callable = getattr(sandbox_obj, "is_running", None)
    if not callable(is_running_callable):
        return True

    try:
        probe_result = await _run_blocking_sandbox_call(
            'is_running',
            lambda: is_running_callable(),
            timeout_seconds=SANDBOX_IO_TIMEOUT_SECONDS,
            sandbox_id=sandbox_id,
        )
        if inspect.isawaitable(probe_result):
            probe_result = await asyncio.wait_for(
                probe_result,
                timeout=SANDBOX_IO_TIMEOUT_SECONDS,
            )
        return bool(probe_result)
    except Exception as probe_error:
        logger.warning(
            "Sandbox liveliness probe failed",
            extra={
                "sandbox_id": sandbox_id,
                "error": str(probe_error),
            },
        )
        return False


async def _finalize_sandbox_reactivation(
    client,
    sandbox_obj: Any,
    *,
    sandbox_info: Dict[str, Any],
    sandbox_type: str,
    project_id: str,
    canonical_sandbox_id: str,
    alias_ids: List[str],
    action: str,
    password: Optional[str] = None,
) -> str:
    new_id = _extract_sandbox_object_id(sandbox_obj, canonical_sandbox_id)
    now_iso = datetime.now(timezone.utc).isoformat()

    if sandbox_type == 'desktop':
        try:
            await _run_blocking_sandbox_call(
                'stream.start',
                lambda: sandbox_obj.stream.start(),
                timeout_seconds=SANDBOX_IO_TIMEOUT_SECONDS,
                sandbox_id=new_id,
            )
        except Exception as stream_error:
            if 'already running' not in str(stream_error).lower():
                logger.warning(f"Desktop stream restart after resume: {stream_error}")

    threshold_seconds = _get_preclone_threshold_seconds()
    updated_info = {
        **sandbox_info,
        'state': 'running',
        'id': new_id,
        'last_accessed_at': now_iso,
        'lifecycle_started_at': now_iso,
        'preclone_due_at': _compute_preclone_due_at(datetime.now(timezone.utc), threshold_seconds),
        'preclone_status': 'pending',
        'preclone_fail_count': 0,
    }
    updated_info.pop('paused_at', None)
    updated_info['pause_grace_seconds'] = _get_auto_pause_grace_seconds()
    if action == 'created':
        updated_info['created_at'] = now_iso
        if password:
            updated_info['pass'] = password

    if sandbox_type == 'desktop':
        try:
            vnc_url_raw = await _run_blocking_sandbox_call(
                'stream.get_url',
                lambda: sandbox_obj.stream.get_url(),
                timeout_seconds=SANDBOX_IO_TIMEOUT_SECONDS,
                sandbox_id=new_id,
            )
            vnc_url = _extract_stream_url(vnc_url_raw)
            updated_info['vnc_preview'] = vnc_url
            updated_info['sandbox_url'] = vnc_url
        except Exception:
            pass

    updated_info = _merge_sandbox_template_lineage(
        updated_info,
        sandbox_type=sandbox_type,
        sandbox_obj=sandbox_obj,
        fallback_template_id=_normalize_template_value(sandbox_info.get("template_id")),
        allow_expected_fallback=(action == "created"),
    )

    await client.table('projects').eq('project_id', project_id).update({
        'sandbox': json.dumps(updated_info)
    })

    if new_id != canonical_sandbox_id:
        for old_id in [*alias_ids, canonical_sandbox_id]:
            await _store_sandbox_id_mapping(old_id, new_id)

    logger.info(f"Sandbox {action} from stale state for project {project_id}: {new_id}")
    if action == 'created' or new_id != canonical_sandbox_id:
        try:
            await _rehydrate_workspace_artifacts_into_sandbox(
                client,
                project_id=project_id,
                sandbox_obj=sandbox_obj,
                sandbox_id=new_id,
            )
        except Exception as rehydrate_error:
            logger.warning(
                "Workspace artifact rehydrate failed for project=%s sandbox=%s: %s",
                project_id,
                new_id,
                rehydrate_error,
            )
    return new_id


async def _resume_or_create_and_finalize(
    client,
    *,
    project_id: str,
    sandbox_type: str,
    sandbox_info: Dict[str, Any],
    canonical_sandbox_id: str,
    alias_ids: List[str],
    password: str,
) -> Any:
    sandbox_obj, action = await resume_or_create_sandbox(
        password=password,
        project_id=project_id,
        sandbox_type=sandbox_type,
        sandbox_info=sandbox_info,
        reattach_budget_seconds=_get_reattach_budget_seconds(),
        allow_create=False,
    )
    resolved_id = _extract_sandbox_object_id(sandbox_obj, canonical_sandbox_id)
    if not await _probe_sandbox_is_running(sandbox_obj, sandbox_id=resolved_id):
        raise SandboxOriginalReuseExhausted(
            canonical_sandbox_id,
            sandbox_type,
            project_id,
            last_error=RuntimeError(
                f"Sandbox reactivation returned non-running handle: {resolved_id}"
            ),
        )
    await _finalize_sandbox_reactivation(
        client,
        sandbox_obj,
        sandbox_info=sandbox_info,
        sandbox_type=sandbox_type,
        project_id=project_id,
        canonical_sandbox_id=canonical_sandbox_id,
        alias_ids=alias_ids,
        action=action,
        password=password,
    )
    return sandbox_obj


async def _maybe_preclone_sandbox_context(
    client,
    *,
    project_id: str,
    sandbox_info: Dict[str, Any],
    sandbox_type: str,
    canonical_sandbox_id: str,
    alias_ids: List[str],
) -> Dict[str, Any]:
    if not _is_preclone_enabled():
        return {
            'sandbox_info': sandbox_info,
            'sandbox_id': canonical_sandbox_id,
            'alias_ids': alias_ids,
        }

    now = datetime.now(timezone.utc)
    threshold_seconds = _get_preclone_threshold_seconds()
    current_info = _ensure_sandbox_lifecycle_fields(
        sandbox_info,
        now=now,
        threshold_seconds=threshold_seconds,
    )
    current_sandbox_id = str(current_info.get('id') or canonical_sandbox_id or '').strip()
    if not current_sandbox_id:
        return {
            'sandbox_info': current_info,
            'sandbox_id': canonical_sandbox_id,
            'alias_ids': alias_ids,
        }
    if current_info.get('state') == 'paused':
        return {
            'sandbox_info': current_info,
            'sandbox_id': current_sandbox_id,
            'alias_ids': alias_ids,
        }

    due_at = _parse_iso_datetime(current_info.get('preclone_due_at'))
    age_seconds = _get_sandbox_age_seconds(current_info, now)
    if due_at and due_at > now:
        return {
            'sandbox_info': current_info,
            'sandbox_id': current_sandbox_id,
            'alias_ids': alias_ids,
        }
    if age_seconds < threshold_seconds:
        return {
            'sandbox_info': current_info,
            'sandbox_id': current_sandbox_id,
            'alias_ids': alias_ids,
        }

    preclone_lock = await _get_project_preclone_lock(project_id)
    async with preclone_lock:
        latest_row = await client.table('projects').select('sandbox').eq('project_id', project_id).execute()
        if not latest_row.data:
            return {
                'sandbox_info': current_info,
                'sandbox_id': current_sandbox_id,
                'alias_ids': alias_ids,
            }

        latest_info = _parse_sandbox_info(latest_row.data[0].get('sandbox', {}))
        latest_info = _ensure_sandbox_lifecycle_fields(
            latest_info,
            now=datetime.now(timezone.utc),
            threshold_seconds=threshold_seconds,
        )
        latest_id = str(latest_info.get('id') or '').strip()
        if not latest_id:
            return {
                'sandbox_info': latest_info,
                'sandbox_id': current_sandbox_id,
                'alias_ids': alias_ids,
            }
        if latest_id != current_sandbox_id:
            for old_id in [*alias_ids, canonical_sandbox_id, current_sandbox_id]:
                await _store_sandbox_id_mapping(old_id, latest_id)
            return {
                'sandbox_info': latest_info,
                'sandbox_id': latest_id,
                'alias_ids': list(dict.fromkeys([*alias_ids, current_sandbox_id, latest_id])),
            }
        if latest_info.get('state') == 'paused':
            return {
                'sandbox_info': latest_info,
                'sandbox_id': latest_id,
                'alias_ids': alias_ids,
            }

        now = datetime.now(timezone.utc)
        due_at = _parse_iso_datetime(latest_info.get('preclone_due_at'))
        age_seconds = _get_sandbox_age_seconds(latest_info, now)
        if due_at and due_at > now:
            return {
                'sandbox_info': latest_info,
                'sandbox_id': latest_id,
                'alias_ids': alias_ids,
            }
        if age_seconds < threshold_seconds:
            return {
                'sandbox_info': latest_info,
                'sandbox_id': latest_id,
                'alias_ids': alias_ids,
            }

        clone_timeout_seconds = _get_preclone_clone_timeout_seconds()
        try:
            new_sandbox_id, snapshot_template_id = await clone_sandbox(
                latest_id,
                count=1,
                timeout=clone_timeout_seconds,
            )
            now_iso = datetime.now(timezone.utc).isoformat()
            updated_info = {
                **latest_info,
                'id': str(new_sandbox_id),
                'state': 'running',
                'created_at': now_iso,
                'last_clone_at': now_iso,
                'clone_parent_id': latest_id,
                'snapshot_template_id': snapshot_template_id,
                'lifecycle_started_at': now_iso,
                'preclone_status': 'ok',
                'preclone_fail_count': 0,
                'preclone_due_at': _compute_preclone_due_at(datetime.now(timezone.utc), threshold_seconds),
            }
            updated_info.pop('paused_at', None)

            await client.table('projects').eq('project_id', project_id).update({
                'sandbox': json.dumps(updated_info)
            })

            for old_id in [*alias_ids, canonical_sandbox_id, latest_id]:
                await _store_sandbox_id_mapping(old_id, str(new_sandbox_id))

            await _schedule_old_sandbox_cleanup(latest_id)
            logger.info(
                "Sandbox preclone rotation completed",
                extra={
                    'project_id': project_id,
                    'source_sandbox_id': latest_id,
                    'new_sandbox_id': str(new_sandbox_id),
                },
            )
            return {
                'sandbox_info': updated_info,
                'sandbox_id': str(new_sandbox_id),
                'alias_ids': list(dict.fromkeys([*alias_ids, latest_id, str(new_sandbox_id)])),
            }
        except Exception as clone_error:
            now = datetime.now(timezone.utc)
            now_iso = now.isoformat()
            retry_seconds = _get_preclone_retry_seconds()
            failed_info = {
                **latest_info,
                'preclone_status': 'failed',
                'preclone_last_failed_at': now_iso,
                'preclone_last_error': str(clone_error),
                'preclone_fail_count': int(latest_info.get('preclone_fail_count') or 0) + 1,
                'preclone_due_at': (now + timedelta(seconds=retry_seconds)).isoformat(),
            }
            try:
                await client.table('projects').eq('project_id', project_id).update({
                    'sandbox': json.dumps(failed_info)
                })
            except Exception:
                pass

            logger.warning(
                "Sandbox preclone failed; continuing with current sandbox",
                extra={
                    'project_id': project_id,
                    'sandbox_id': latest_id,
                    'error': str(clone_error),
                },
            )
            return {
                'sandbox_info': failed_info,
                'sandbox_id': latest_id,
                'alias_ids': alias_ids,
            }


async def _touch_project_sandbox_access(
    client,
    *,
    project_id: Optional[str],
    sandbox_info: Dict[str, Any],
    fallback_sandbox_id: Optional[str] = None,
) -> None:
    if not project_id:
        return

    latest_info = sandbox_info
    try:
        latest_project = await client.table('projects').select('sandbox').eq('project_id', project_id).execute()
        if latest_project.data:
            latest_info = _parse_sandbox_info(latest_project.data[0].get('sandbox', {}))
    except Exception:
        # Fall back to caller-provided metadata when best-effort refresh fails.
        latest_info = sandbox_info

    resolved_sandbox_id = str(
        latest_info.get('id')
        or fallback_sandbox_id
        or ''
    ).strip()
    if not resolved_sandbox_id:
        return

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    threshold_seconds = _get_preclone_threshold_seconds()
    updated_info = _ensure_sandbox_lifecycle_fields(
        {
            **latest_info,
            'id': resolved_sandbox_id,
            'last_accessed_at': now_iso,
            'pause_grace_seconds': _get_auto_pause_grace_seconds(),
        },
        now=now,
        threshold_seconds=threshold_seconds,
    )

    try:
        await client.table('projects').eq('project_id', project_id).update({
            'sandbox': json.dumps(updated_info)
        })
    except Exception as update_error:
        logger.debug(f"Failed to update sandbox access timestamp for project {project_id}: {update_error}")
        return

    if SANDBOX_AUTO_PAUSE_DELAY_ENABLED and _is_auto_pause_enabled():
        await _schedule_project_auto_pause_monitor(project_id, reference_time=now)


async def _run_project_auto_pause_monitor(project_id: str, reference_time: datetime) -> None:
    grace_seconds = _get_auto_pause_grace_seconds()
    inactivity_reference = reference_time

    while True:
        if not _is_auto_pause_enabled():
            return

        if await _project_has_active_run(project_id):
            inactivity_reference = datetime.now(timezone.utc)
            logger.debug(
                "Auto pause monitor skipped due to active run",
                extra={"project_id": project_id},
            )
            await asyncio.sleep(SANDBOX_AUTO_PAUSE_ACTIVE_RUN_POLL_SECONDS)
            continue

        client = await db.client
        project = await client.table('projects').select('sandbox').eq('project_id', project_id).execute()
        if not project.data:
            return

        raw = project.data[0].get('sandbox', '{}')
        sandbox_info = _parse_sandbox_info(raw)
        sandbox_id = str(sandbox_info.get('id') or '').strip()
        if not sandbox_id:
            return
        if sandbox_info.get('state') == 'paused':
            return

        last_accessed_at = _parse_iso_datetime(sandbox_info.get('last_accessed_at'))
        if last_accessed_at and last_accessed_at > inactivity_reference:
            inactivity_reference = last_accessed_at

        if grace_seconds > 0:
            elapsed = (datetime.now(timezone.utc) - inactivity_reference).total_seconds()
            remaining = grace_seconds - elapsed
            if remaining > 0:
                await asyncio.sleep(max(0.5, min(remaining, grace_seconds)))
                continue

        # Re-check once before pausing to avoid racing with a fresh access update.
        project_latest = await client.table('projects').select('sandbox').eq('project_id', project_id).execute()
        if not project_latest.data:
            return
        latest_raw = project_latest.data[0].get('sandbox', '{}')
        latest_info = _parse_sandbox_info(latest_raw)
        latest_id = str(latest_info.get('id') or '').strip()
        if not latest_id or latest_info.get('state') == 'paused':
            return

        latest_access = _parse_iso_datetime(latest_info.get('last_accessed_at'))
        if latest_access and latest_access > inactivity_reference:
            inactivity_reference = latest_access
            continue

        if await _project_has_active_run(project_id):
            inactivity_reference = datetime.now(timezone.utc)
            continue

        paused = await pause_sandbox(latest_id)
        if not paused:
            logger.warning(f"Auto pause skipped; provider pause call failed for sandbox {latest_id}")
            return

        latest_info = {
            **latest_info,
            'state': 'paused',
            'paused_at': datetime.now(timezone.utc).isoformat(),
        }
        await client.table('projects').eq('project_id', project_id).update({
            'sandbox': json.dumps(latest_info)
        })
        logger.info(f"Sandbox {latest_id} paused by inactivity monitor for project {project_id}")

        try:
            mapped = await redis.get(f"{SANDBOX_ID_MAP_PREFIX}{latest_id}")
            if mapped and mapped != latest_id:
                await redis.delete(f"{SANDBOX_ID_MAP_PREFIX}{latest_id}")
                logger.info(f"Cleared stale Redis sandbox mapping {latest_id} -> {mapped}")
        except Exception:
            pass
        return


async def _schedule_project_auto_pause_monitor(
    project_id: str,
    *,
    reference_time: Optional[datetime] = None,
) -> None:
    if not project_id:
        return
    if not SANDBOX_AUTO_PAUSE_DELAY_ENABLED:
        return
    if not _is_auto_pause_enabled():
        return

    reference = reference_time or datetime.now(timezone.utc)
    lock = _get_auto_pause_tasks_lock()
    async with lock:
        existing_task = _sandbox_auto_pause_tasks.get(project_id)
        if existing_task and not existing_task.done():
            return

        task = asyncio.create_task(
            _run_project_auto_pause_monitor(project_id, reference),
            name=f"sandbox-auto-pause:{project_id}",
        )
        _sandbox_auto_pause_tasks[project_id] = task

        def _cleanup_task(done_task: asyncio.Task, *, target_project_id: str = project_id) -> None:
            current = _sandbox_auto_pause_tasks.get(target_project_id)
            if current is done_task:
                _sandbox_auto_pause_tasks.pop(target_project_id, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as monitor_error:
                logger.warning(
                    f"Sandbox auto pause monitor failed for project {target_project_id}: {monitor_error}"
                )

        task.add_done_callback(_cleanup_task)


def _cleanup_temp_file(file_path: str) -> None:
    try:
        os.remove(file_path)
    except FileNotFoundError:
        return
    except Exception as cleanup_error:
        logger.warning(f"Failed to clean up temporary file {file_path}: {cleanup_error}")


def _normalize_workspace_path(path: str) -> str:
    normalized = normalize_path(path)
    if normalized != '/workspace' and not normalized.startswith('/workspace/'):
        raise HTTPException(status_code=400, detail='Path must stay under /workspace')
    return normalized


def _build_archive_filename(sandbox_id: str) -> str:
    date_suffix = datetime.utcnow().strftime('%Y-%m-%d')
    return f"workspace-{sandbox_id}-{date_suffix}.zip"


def _normalize_download_filename(filename: str, *, fallback: str = "download") -> str:
    candidate = os.path.basename(str(filename or "").strip()) or fallback
    sanitized: List[str] = []
    for char in candidate:
        codepoint = ord(char)
        if codepoint < 32 or codepoint == 127 or char in {'"', '\\'}:
            sanitized.append("_")
        else:
            sanitized.append(char)
    normalized = "".join(sanitized).strip()
    return normalized or fallback


def _ascii_download_filename(filename: str, *, fallback: str = "download") -> str:
    candidate = _normalize_download_filename(filename, fallback=fallback)
    ascii_candidate = candidate.encode("ascii", "ignore").decode("ascii").strip(" .")
    ascii_candidate = ascii_candidate.replace('"', "_").replace("\\", "_")
    return ascii_candidate or fallback


def _build_content_disposition(filename: str) -> str:
    normalized_filename = _normalize_download_filename(filename)
    ascii_filename = _ascii_download_filename(normalized_filename)
    encoded_filename = urllib.parse.quote(normalized_filename, safe="")
    return (
        f"attachment; filename=\"{ascii_filename}\"; "
        f"filename*=UTF-8''{encoded_filename}"
    )


def _extract_request_id(request: Optional[Request]) -> Optional[str]:
    if request is not None:
        try:
            request_id = str(request.headers.get("x-request-id") or "").strip()
            if request_id:
                return request_id
        except Exception:
            pass
        try:
            request_id = str(getattr(request.state, "request_id", "") or "").strip()
            if request_id:
                return request_id
        except Exception:
            pass

    try:
        import structlog

        contextvars_module = getattr(structlog, "contextvars", None)
        get_contextvars = getattr(contextvars_module, "get_contextvars", None)
        if callable(get_contextvars):
            request_id = str(get_contextvars().get("request_id") or "").strip()
            if request_id:
                return request_id
    except Exception:
        pass

    return None


def _extract_client_operation_id(request: Optional[Request]) -> Optional[str]:
    if request is None:
        return None

    try:
        operation_id = str(request.headers.get("x-client-operation-id") or "").strip()
        if operation_id:
            return operation_id
    except Exception:
        pass

    return None


def _build_request_correlation_fields(request: Optional[Request]) -> Dict[str, Optional[str]]:
    return {
        "request_id": _extract_request_id(request),
        "client_operation_id": _extract_client_operation_id(request),
    }


def _build_file_action_log_extra(
    request: Optional[Request],
    **extra: Any,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = dict(_build_request_correlation_fields(request))
    payload.update(extra)
    return payload


def _log_file_action_event(
    level: str,
    event: str,
    *,
    request: Optional[Request],
    **extra: Any,
) -> None:
    log_method = getattr(logger, level, logger.info)
    log_method(event, extra=_build_file_action_log_extra(request, **extra))


def _response_header_value(headers: Any, key: str) -> Optional[str]:
    try:
        value = headers.get(key)
        if value is not None:
            return str(value)
    except Exception:
        pass

    try:
        value = headers.get(key.lower())
        if value is not None:
            return str(value)
    except Exception:
        pass

    return None


def _response_content_length(response: Response) -> Optional[int]:
    payload = getattr(response, "body", None)
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)

    payload = getattr(response, "content", None)
    if isinstance(payload, (bytes, bytearray)):
        return len(payload)

    return None


def _with_request_correlation_headers(
    headers: Dict[str, str],
    *,
    request: Optional[Request],
) -> Dict[str, str]:
    request_id = _extract_request_id(request)
    if request_id:
        headers["X-Request-Id"] = request_id
    client_operation_id = _extract_client_operation_id(request)
    if client_operation_id:
        headers[CLIENT_OPERATION_ID_HEADER] = client_operation_id
    return headers


def _build_file_download_headers(
    *,
    filename: str,
    request: Optional[Request],
    response_source: str,
) -> Dict[str, str]:
    return _with_request_correlation_headers(
        {
            "Content-Disposition": _build_content_disposition(filename),
            "X-Workspace-Response-Source": response_source,
            "X-Workspace-Fallback": "1" if response_source != "sandbox" else "0",
        },
        request=request,
    )


def _classify_archive_response_source(
    *,
    live_success_count: int,
    artifact_success_count: int,
) -> str:
    if live_success_count and artifact_success_count:
        return "mixed"
    if artifact_success_count:
        return "artifact"
    return "sandbox"


def _build_archive_download_headers(
    *,
    request: Optional[Request],
    filename: str,
    total_count: int,
    succeeded_count: int,
    failed_count: int,
    response_source: str,
    identity_source: str,
    sandbox_available: bool,
    outcome: str,
) -> Dict[str, str]:
    return _with_request_correlation_headers(
        {
            "Content-Disposition": _build_content_disposition(filename),
            "X-Archive-Total": str(total_count),
            "X-Archive-Succeeded": str(succeeded_count),
            "X-Archive-Failed": str(failed_count),
            "X-Archive-Response-Source": response_source,
            "X-Archive-Fallback": "1" if response_source != "sandbox" else "0",
            "X-Archive-Identity-Source": identity_source,
            "X-Archive-Sandbox-Available": "1" if sandbox_available else "0",
            "X-Archive-Outcome": outcome,
        },
        request=request,
    )


def _build_archive_observability_headers(
    *,
    request: Optional[Request],
    response_source: Optional[str],
    identity_source: Optional[str],
    sandbox_available: Optional[bool],
    outcome: Optional[str],
) -> Dict[str, str]:
    headers: Dict[str, str] = {}
    if response_source:
        headers["X-Archive-Response-Source"] = response_source
        headers["X-Archive-Fallback"] = "1" if response_source != "sandbox" else "0"
    if identity_source:
        headers["X-Archive-Identity-Source"] = identity_source
    if sandbox_available is not None:
        headers["X-Archive-Sandbox-Available"] = "1" if sandbox_available else "0"
    if outcome:
        headers["X-Archive-Outcome"] = outcome
    return _with_request_correlation_headers(headers, request=request)


class ArchiveDownloadRequest(BaseModel):
    root_path: Optional[str] = '/workspace'
    paths: Optional[List[str]] = None
    include_hidden: Optional[bool] = False
    continue_on_error: Optional[bool] = True



def _is_hidden_workspace_path(path: str) -> bool:
    try:
        return is_hidden_workspace_artifact_path(path)
    except Exception:
        return True


def _is_user_visible_workspace_path(path: str) -> bool:
    try:
        return is_user_visible_workspace_artifact_path(path)
    except Exception:
        return False



_CLAUDE_LOCAL_SANDBOX_PREFIX = "claude-local:"
_THREAD_WORKSPACE_SANDBOX_PREFIX = "thread-workspace:"


def _parse_claude_local_agent_run_id(*sandbox_ids: Optional[str]) -> Optional[str]:
    for sandbox_id in sandbox_ids:
        normalized = str(sandbox_id or "").strip()
        if normalized.startswith(_CLAUDE_LOCAL_SANDBOX_PREFIX):
            run_id = normalized[len(_CLAUDE_LOCAL_SANDBOX_PREFIX):].strip()
            if run_id:
                return run_id
    return None


def _parse_thread_workspace_thread_id(*sandbox_ids: Optional[str]) -> Optional[str]:
    for sandbox_id in sandbox_ids:
        normalized = str(sandbox_id or "").strip()
        if normalized.startswith(_THREAD_WORKSPACE_SANDBOX_PREFIX):
            thread_id = normalized[len(_THREAD_WORKSPACE_SANDBOX_PREFIX):].strip()
            if thread_id:
                return thread_id
    return None


def _artifact_scope_for_agent_run(agent_run_id: Optional[str]) -> Optional[str]:
    return "agent_run" if agent_run_id else None


async def _resolve_thread_workspace_context(
    client: Any,
    *,
    thread_id: str,
    user_id: Optional[str],
) -> tuple[str, str]:
    thread_access = await verify_thread_access(client, thread_id, user_id)
    if isinstance(thread_access, dict):
        project_id = str(thread_access.get("project_id") or "").strip()
    else:
        thread_result = await (
            client
            .table("threads")
            .select("*")
            .eq("thread_id", thread_id)
            .execute()
        )
        if not getattr(thread_result, "data", None):
            raise HTTPException(status_code=404, detail="Thread not found")
        project_id = str((thread_result.data[0] or {}).get("project_id") or "").strip()
    if not project_id:
        raise HTTPException(status_code=404, detail="Thread project not found")
    return project_id, "thread_workspace_artifacts"


async def _list_thread_workspace_artifact_entries_for_path(
    client: Any,
    *,
    project_id: str,
    thread_id: str,
    path: str,
    include_hidden: bool,
) -> List[Dict[str, Any]]:
    if not _is_user_visible_workspace_path(path) and not include_hidden:
        return []
    entries = await workspace_artifacts.list_thread_entries(
        project_id=project_id,
        thread_id=thread_id,
        path=path,
        client=client,
        include_hidden=include_hidden,
    )
    return [_artifact_entry_to_file_info(entry) for entry in entries]


async def _read_thread_workspace_artifact_response(
    client: Any,
    *,
    project_id: str,
    thread_id: str,
    path: str,
    include_hidden: bool = False,
    request: Request = None,
) -> Optional[Response]:
    if not _is_user_visible_workspace_path(path) and not include_hidden:
        return None
    artifact = await workspace_artifacts.read_thread_artifact_bytes(
        project_id=project_id,
        thread_id=thread_id,
        path=path,
        client=client,
        include_hidden=include_hidden,
    )
    if artifact is None:
        return None
    record, data = artifact
    filename = os.path.basename(path)
    return Response(
        content=data,
        media_type=guess_workspace_artifact_content_type(path, data, record.content_type),
        headers={
            **_build_file_download_headers(
                filename=filename,
                request=request,
                response_source="thread_workspace",
            ),
            "x-workspace-artifact-scope": "thread",
            "x-workspace-thread-id": str(thread_id),
        },
    )


def _thread_workspace_not_found_http_exception(
    *,
    path: str,
    identity_source: str,
    thread_id: str,
) -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={
            "message": "Thread workspace artifact not found",
            "error_code": "THREAD_WORKSPACE_ARTIFACT_NOT_FOUND",
            "path": path,
            "identity_source": identity_source,
            "artifact_scope": "thread",
            "thread_id": thread_id,
            "artifact_store_state": workspace_artifacts.availability_state(),
        },
    )

def _artifact_entry_to_file_info(entry: Dict[str, Any]) -> Dict[str, Any]:
    is_dir = bool(entry.get("is_dir"))
    delivery_source = str(entry.get("delivery_source") or "artifact")
    downloadable = entry.get("downloadable")
    if downloadable is None:
        downloadable = not is_dir
    artifact_state = entry.get("artifact_state")
    if artifact_state is None and delivery_source == "artifact" and not is_dir:
        artifact_state = "available"

    return FileInfo(
        name=str(entry.get("name") or ""),
        path=str(entry.get("path") or ""),
        is_dir=is_dir,
        size=int(entry.get("size") or 0),
        mod_time=str(entry.get("mod_time") or ""),
        permissions=entry.get("permissions"),
        delivery_source=delivery_source,
        downloadable=bool(downloadable),
        artifact_state=artifact_state,
        artifact_source=entry.get("artifact_source") or entry.get("source"),
    ).model_dump(exclude_none=True)


def _sandbox_entry_to_file_info(file: Any, full_path: str) -> Dict[str, Any]:
    is_dir = _is_dir_entry(file)
    return FileInfo(
        name=file.name,
        path=full_path,
        is_dir=is_dir,
        size=getattr(file, 'size', 0),
        mod_time=_entry_mod_time(file),
        permissions=getattr(file, 'permissions', None),
        delivery_source="sandbox",
        downloadable=not is_dir,
    ).model_dump(exclude_none=True)


def _merge_workspace_file_infos(
    sandbox_files: List[Dict[str, Any]],
    artifact_entries: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for entry in artifact_entries:
        path = str(entry.get("path") or "")
        if path:
            merged[path] = dict(entry)
    for entry in sandbox_files:
        path = str(entry.get("path") or "")
        if path:
            merged[path] = dict(entry)
    return sorted(
        merged.values(),
        key=lambda item: (not bool(item.get("is_dir")), str(item.get("name") or "").lower()),
    )


async def _list_workspace_artifact_entries_for_path(
    client: Any,
    *,
    project_id: Optional[str],
    path: str,
    include_hidden: bool,
    agent_run_id: Optional[str] = None,
    sandbox_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not project_id:
        return []
    if not _is_user_visible_workspace_path(path) and not include_hidden:
        return []
    entries = await workspace_artifacts.list_entries(
        project_id=project_id,
        path=path,
        client=client,
        include_hidden=include_hidden,
        agent_run_id=agent_run_id,
        sandbox_id=sandbox_id,
    )
    return [_artifact_entry_to_file_info(entry) for entry in entries]


async def _read_workspace_artifact_response(
    client: Any,
    *,
    project_id: Optional[str],
    path: str,
    include_hidden: bool = False,
    request: Request = None,
    agent_run_id: Optional[str] = None,
    sandbox_id: Optional[str] = None,
) -> Optional[Response]:
    if not project_id:
        return None
    if not _is_user_visible_workspace_path(path) and not include_hidden:
        return None

    artifact = await workspace_artifacts.read_artifact_bytes(
        project_id=project_id,
        path=path,
        client=client,
        include_hidden=include_hidden,
        agent_run_id=agent_run_id,
        sandbox_id=sandbox_id,
    )
    if artifact is None:
        return None

    record, data = artifact
    filename = os.path.basename(path)
    return Response(
        content=data,
        media_type=guess_workspace_artifact_content_type(path, data, record.content_type),
        headers={
            **_build_file_download_headers(
                filename=filename,
                request=request,
                response_source="artifact",
            ),
            **({"x-workspace-artifact-scope": "agent_run"} if agent_run_id else {}),
            **({"x-workspace-agent-run-id": str(agent_run_id)} if agent_run_id else {}),
        },
    )


def _build_workspace_directory_http_exception(
    *,
    path: str,
    identity_source: str,
    sandbox_available: bool,
    directory_source: str,
    artifact_scope: Optional[str] = None,
    agent_run_id: Optional[str] = None,
) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "message": "Requested path is a directory. Use the files listing or archive endpoint instead.",
            "error_code": "WORKSPACE_PATH_IS_DIRECTORY",
            "path": path,
            "path_kind": "directory",
            "downloadable": False,
            "identity_source": identity_source,
            "sandbox_available": sandbox_available,
            "directory_source": directory_source,
            "artifact_store_state": workspace_artifacts.availability_state(),
            **({"artifact_scope": artifact_scope} if artifact_scope else {}),
            **({"agent_run_id": agent_run_id} if agent_run_id else {}),
        },
    )


async def _maybe_build_workspace_directory_http_exception(
    client: Any,
    *,
    project_id: Optional[str],
    path: str,
    include_hidden: bool,
    identity_source: str,
    sandbox_available: bool,
    agent_run_id: Optional[str] = None,
) -> Optional[HTTPException]:
    if not path.startswith("/workspace"):
        return None
    if not str(project_id or "").strip():
        return None

    try:
        artifact_entries = await _list_workspace_artifact_entries_for_path(
            client,
            project_id=project_id,
            path=path,
            include_hidden=include_hidden,
            agent_run_id=agent_run_id,
        )
    except Exception as artifact_error:
        logger.debug(f"Workspace artifact directory probe failed: {artifact_error}")
        return None

    if not artifact_entries:
        return None

    return _build_workspace_directory_http_exception(
        path=path,
        identity_source=identity_source,
        sandbox_available=sandbox_available,
        directory_source="artifact",
        artifact_scope=_artifact_scope_for_agent_run(agent_run_id),
        agent_run_id=agent_run_id,
    )


def _build_list_files_response(
    *,
    files: List[Dict[str, Any]],
    identity_source: str,
    sandbox_available: bool,
    response_source: str,
    sandbox_file_count: int,
    artifact_file_count: int,
    artifact_scope: Optional[str] = None,
    agent_run_id: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "files": files,
        "diagnostics": {
            "identity_source": identity_source,
            "sandbox_available": sandbox_available,
            "response_source": response_source,
            "file_count": len(files),
            "sandbox_file_count": sandbox_file_count,
            "artifact_file_count": artifact_file_count,
            "artifact_store_state": workspace_artifacts.availability_state(),
            **({"artifact_scope": artifact_scope} if artifact_scope else {}),
            **({"agent_run_id": agent_run_id} if agent_run_id else {}),
            **({"thread_id": thread_id} if thread_id else {}),
        },
    }


def _is_workspace_artifact_only_binding_state(binding_state: Optional[str]) -> bool:
    normalized = _normalize_shadow_clone_binding_state(binding_state)
    return normalized in {"released", "lost"}


def _build_workspace_artifact_not_found_http_exception(
    *,
    path: str,
    identity_source: str,
    sandbox_available: bool,
    binding_state: Optional[str] = None,
    artifact_scope: Optional[str] = None,
    agent_run_id: Optional[str] = None,
) -> HTTPException:
    detail: Dict[str, Any] = {
        "message": "Workspace files could not be resolved because the live sandbox is unavailable and no durable artifacts were found.",
        "error_code": "WORKSPACE_ARTIFACT_FALLBACK_NOT_FOUND",
        "path": path,
        "identity_source": identity_source,
        "sandbox_available": sandbox_available,
        "artifact_store_state": workspace_artifacts.availability_state(),
        **({"artifact_scope": artifact_scope} if artifact_scope else {}),
        **({"agent_run_id": agent_run_id} if agent_run_id else {}),
    }
    normalized_binding_state = _normalize_shadow_clone_binding_state(binding_state)
    if normalized_binding_state:
        detail["binding_state"] = normalized_binding_state
    return HTTPException(status_code=404, detail=detail)


def _can_use_workspace_artifact_fallback(
    *,
    path: str,
    project_id: Optional[str],
    http_error: Optional[HTTPException] = None,
) -> bool:
    if not path.startswith("/workspace"):
        return False
    if not str(project_id or "").strip():
        return False
    if http_error is not None and http_error.status_code in {401, 403}:
        return False
    if http_error is not None:
        detail = getattr(http_error, "detail", None)
        if isinstance(detail, dict) and detail.get("error_code") == "WORKSPACE_PATH_IS_DIRECTORY":
            return False
        if isinstance(detail, dict) and detail.get("error_code") == "WORKSPACE_ARTIFACT_FALLBACK_NOT_FOUND":
            return False
    return True


async def _rehydrate_workspace_artifacts_into_sandbox(
    client: Any,
    *,
    project_id: str,
    sandbox_obj: Any,
    sandbox_id: str,
) -> None:
    if not bool(getattr(config, "WORKSPACE_ARTIFACTS_REHYDRATE_ON_RECOVERY", True)):
        return

    async def _make_dir(path: str) -> None:
        await _run_guarded_sandbox_io(
            sandbox_id,
            'files.make_dir',
            lambda current=path: sandbox_obj.files.make_dir(current),
            path=path,
        )

    async def _write_file(path: str, data: bytes) -> None:
        await _run_guarded_sandbox_io(
            sandbox_id,
            'files.write',
            lambda current=path, payload=data: sandbox_obj.files.write(current, payload),
            path=path,
        )

    result = await workspace_artifacts.rehydrate_project(
        project_id=project_id,
        client=client,
        make_dir=_make_dir,
        write_file=_write_file,
    )
    if result.get("total"):
        logger.info(
            "Workspace artifact rehydrate completed for project=%s sandbox=%s: %s/%s files restored",
            project_id,
            sandbox_id,
            result.get("rehydrated", 0),
            result.get("total", 0),
        )


async def _resolve_sandbox_id(sandbox_id: str) -> str:
    if not sandbox_id:
        return sandbox_id

    current_id = str(sandbox_id).strip()
    seen_ids: List[str] = []

    for _ in range(SANDBOX_ID_MAP_MAX_HOPS):
        seen_ids.append(current_id)
        try:
            mapped = await redis.get(f"{SANDBOX_ID_MAP_PREFIX}{current_id}")
        except Exception as e:
            logger.warning(f"Sandbox id mapping lookup failed: {e}")
            break

        if isinstance(mapped, bytes):
            mapped = mapped.decode("utf-8", errors="replace")
        mapped_id = str(mapped or "").strip()
        if not mapped_id or mapped_id == current_id:
            break

        if mapped_id in seen_ids:
            logger.warning(
                "Detected sandbox id mapping cycle while resolving %s: %s -> %s",
                sandbox_id,
                " -> ".join(seen_ids),
                mapped_id,
            )
            break

        current_id = mapped_id
    else:
        logger.warning(
            "Sandbox id mapping exceeded max hops while resolving %s after %s hops",
            sandbox_id,
            SANDBOX_ID_MAP_MAX_HOPS,
        )

    if current_id != sandbox_id:
        logger.info(
            "Resolved sandbox id chain %s",
            " -> ".join(seen_ids),
        )
        aliases_to_flatten = [alias for alias in seen_ids if alias != current_id]
        for alias in aliases_to_flatten:
            await _store_sandbox_id_mapping(alias, current_id)

    return current_id


async def _store_sandbox_id_mapping(old_id: str, new_id: str) -> None:
    if not old_id or not new_id or old_id == new_id:
        return
    try:
        await redis.set(
            f"{SANDBOX_ID_MAP_PREFIX}{old_id}",
            str(new_id),
            ex=SANDBOX_ID_MAP_TTL,
        )
    except Exception as e:
        logger.warning(f"Failed to store sandbox id mapping {old_id} -> {new_id}: {e}")


def initialize(_db: DBConnection):
    """Initialize the sandbox API with resources from the main API."""
    global db
    db = _db
    logger.info("Initialized sandbox API with database connection")

class FileInfo(BaseModel):
    """Model for file information"""
    name: str
    path: str
    is_dir: bool
    size: int
    mod_time: str
    permissions: Optional[str] = None
    delivery_source: Optional[str] = None
    downloadable: Optional[bool] = None
    artifact_state: Optional[str] = None
    artifact_source: Optional[str] = None

def normalize_path(path: str) -> str:
    """
    Normalize a path to ensure proper UTF-8 encoding and handling.
    
    Args:
        path: The file path, potentially containing URL-encoded characters
        
    Returns:
        Normalized path with proper UTF-8 encoding
    """
    try:
        # First, ensure the path is properly URL-decoded
        decoded_path = urllib.parse.unquote(path)
        
        # Handle Unicode escape sequences like \u0308
        try:
            # Replace Python-style Unicode escapes (\u0308) with actual characters
            # This handles cases where the Unicode escape sequence is part of the URL
            import re
            unicode_pattern = re.compile(r'\\u([0-9a-fA-F]{4})')
            
            def replace_unicode(match):
                hex_val = match.group(1)
                return chr(int(hex_val, 16))
            
            decoded_path = unicode_pattern.sub(replace_unicode, decoded_path)
        except Exception as unicode_err:
            logger.warning(f"Error processing Unicode escapes in path '{path}': {str(unicode_err)}")
        
        logger.debug(f"Normalized path from '{path}' to '{decoded_path}'")
        return decoded_path
    except Exception as e:
        logger.error(f"Error normalizing path '{path}': {str(e)}")
        return path  # Return original path if decoding fails

async def verify_sandbox_access(client, sandbox_id: str, user_id: Optional[str] = None, original_sandbox_id: Optional[str] = None):
    """
    Verify that a user has access to a specific sandbox based on account membership.

    Args:
        client: The Supabase client
        sandbox_id: The sandbox ID to check access for
        user_id: The user ID to check permissions for. Can be None for public resource access.
        original_sandbox_id: The pre-resolved sandbox ID. When the DB still references the
            old ID after a sandbox recreation, this fallback prevents spurious 404s.

    Returns:
        dict: Project data containing sandbox information

    Raises:
        HTTPException: If the user doesn't have access to the sandbox or sandbox doesn't exist
    """
    # Find the project that owns this sandbox
    # The sandbox column may be stored as TEXT; cast to JSONB before extracting id
    project_result = await (
        client
        .table('projects')
        .select('*')
        .eq("(sandbox::jsonb ->> 'id')", sandbox_id)
        .execute()
    )

    # Fallback: if the DB still has the old (pre-resolved) sandbox ID, try that
    if not project_result.data and original_sandbox_id and original_sandbox_id != sandbox_id:
        project_result = await (
            client
            .table('projects')
            .select('*')
            .eq("(sandbox::jsonb ->> 'id')", original_sandbox_id)
            .execute()
        )

    if not project_result.data or len(project_result.data) == 0:
        raise HTTPException(status_code=404, detail="Sandbox not found")
    
    project_data = project_result.data[0]

    if project_data.get('is_public'):
        return project_data
    
    # For private projects, we must have a user_id
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for this resource")
    
    # Direct ownership check: in this project, account_id directly stores user_id
    account_id = project_data.get('account_id')
    if account_id and account_id == user_id:
        return project_data

    raise HTTPException(status_code=403, detail="Not authorized to access this sandbox")


def _verify_project_access_by_row(project_data: Dict[str, Any], user_id: Optional[str] = None) -> Dict[str, Any]:
    if project_data.get('is_public'):
        return project_data

    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for this resource")

    account_id = project_data.get('account_id')
    if account_id and account_id == user_id:
        return project_data

    raise HTTPException(status_code=403, detail="Not authorized to access this sandbox")


async def _load_project_by_id(client, project_id: str) -> Dict[str, Any]:
    project_result = await (
        client
        .table('projects')
        .select('*')
        .eq('project_id', project_id)
        .execute()
    )
    if not project_result.data or len(project_result.data) == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return project_result.data[0]


def _build_archive_identity_failure_detail(
    *,
    requested_sandbox_id: str,
    resolved_sandbox_id: str,
    original_sandbox_id: Optional[str],
    artifact_store_state: str,
) -> Dict[str, Any]:
    return {
        "message": (
            "Archive request could not be mapped to a project sandbox context. "
            "Durable artifact lookup could not safely recover the project."
        ),
        "error_code": "ARCHIVE_PROJECT_CONTEXT_NOT_FOUND",
        "requested_sandbox_id": requested_sandbox_id,
        "resolved_sandbox_id": resolved_sandbox_id,
        "original_sandbox_id": original_sandbox_id,
        "artifact_store_state": artifact_store_state,
    }


async def _resolve_archive_project_context(
    client,
    *,
    requested_sandbox_id: str,
    resolved_sandbox_id: str,
    original_sandbox_id: Optional[str],
    user_id: Optional[str],
) -> tuple[Dict[str, Any], str]:
    try:
        project_data = await verify_sandbox_access(
            client,
            resolved_sandbox_id,
            user_id,
            original_sandbox_id=original_sandbox_id,
        )
        return project_data, "sandbox_access"
    except HTTPException as access_error:
        if access_error.status_code != 404:
            raise

    artifact_hint = None
    candidate_sandbox_ids: List[str] = []
    for candidate in (resolved_sandbox_id, original_sandbox_id, requested_sandbox_id):
        normalized = str(candidate or "").strip()
        if normalized and normalized not in candidate_sandbox_ids:
            candidate_sandbox_ids.append(normalized)

    try:
        artifact_hint = await workspace_artifacts.resolve_project_for_sandbox_hints(
            sandbox_ids=candidate_sandbox_ids,
            client=client,
        )
    except Exception as artifact_error:
        logger.warning(
            "Archive artifact-based project lookup failed",
            extra={
                "requested_sandbox_id": requested_sandbox_id,
                "resolved_sandbox_id": resolved_sandbox_id,
                "original_sandbox_id": original_sandbox_id,
                "candidate_sandbox_ids": candidate_sandbox_ids,
                "artifact_store_state": workspace_artifacts.availability_state(),
                "error": str(artifact_error),
            },
        )

    if artifact_hint is None:
        detail = _build_archive_identity_failure_detail(
            requested_sandbox_id=requested_sandbox_id,
            resolved_sandbox_id=resolved_sandbox_id,
            original_sandbox_id=original_sandbox_id,
            artifact_store_state=workspace_artifacts.availability_state(),
        )
        logger.warning("Archive project context resolution failed", extra=detail)
        raise HTTPException(status_code=404, detail=detail)

    project_data = await _load_project_by_id(client, artifact_hint["project_id"])
    _verify_project_access_by_row(project_data, user_id)

    logger.info(
        "Archive project context resolved via workspace artifacts",
        extra={
            "requested_sandbox_id": requested_sandbox_id,
            "resolved_sandbox_id": resolved_sandbox_id,
            "original_sandbox_id": original_sandbox_id,
            "artifact_hint_sandbox_id": artifact_hint.get("sandbox_id"),
            "project_id": artifact_hint.get("project_id"),
            "artifact_store_state": workspace_artifacts.availability_state(),
        },
    )
    return project_data, "artifact_hint"

def _parse_sandbox_info(raw_sandbox: Any) -> Dict[str, Any]:
    sandbox_info = raw_sandbox or {}
    if isinstance(sandbox_info, str):
        try:
            sandbox_info = json.loads(sandbox_info)
        except json.JSONDecodeError:
            sandbox_info = {}
    if not isinstance(sandbox_info, dict):
        return {}
    return sandbox_info


def _build_sandbox_response_payload(sandbox_info: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(sandbox_info, dict):
        return {}
    return {
        key: value
        for key, value in sandbox_info.items()
        if value is not None
    }


async def _fetch_project_row_by_sandbox_id(client, sandbox_id: str) -> Optional[Dict[str, Any]]:
    if not sandbox_id:
        return None

    project_result = await (
        client
        .table('projects')
        .select('project_id, sandbox')
        .eq("(sandbox::jsonb ->> 'id')", sandbox_id)
        .execute()
    )

    if not project_result.data:
        return None
    return project_result.data[0]


async def _load_project_sandbox_context(client, requested_sandbox_id: str) -> Dict[str, Any]:
    resolved_sandbox_id = await _resolve_sandbox_id(requested_sandbox_id)
    candidate_ids: List[str] = []
    for candidate in (resolved_sandbox_id, requested_sandbox_id):
        if candidate and candidate not in candidate_ids:
            candidate_ids.append(candidate)

    project_row: Optional[Dict[str, Any]] = None
    matched_sandbox_id: Optional[str] = None
    for candidate_id in candidate_ids:
        project_row = await _fetch_project_row_by_sandbox_id(client, candidate_id)
        if project_row:
            matched_sandbox_id = candidate_id
            break

    if not project_row:
        logger.error(f"No project found for sandbox ID: {requested_sandbox_id}")
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Sandbox not found - no project owns this sandbox ID",
                "error_code": "SANDBOX_NOT_FOUND",
                "recoverable": False,
            },
        )

    project_id = project_row.get('project_id')
    sandbox_info = _parse_sandbox_info(project_row.get('sandbox', {}))
    canonical_sandbox_id = str(
        sandbox_info.get('id')
        or resolved_sandbox_id
        or requested_sandbox_id
        or ''
    ).strip()
    if not canonical_sandbox_id:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Project sandbox metadata does not contain a valid sandbox id",
                "error_code": "SANDBOX_METADATA_INVALID",
                "recoverable": False,
            },
        )

    sandbox_type = str(sandbox_info.get('type', 'desktop') or 'desktop')

    alias_ids: List[str] = []
    for alias in (requested_sandbox_id, resolved_sandbox_id, matched_sandbox_id):
        if alias and alias not in alias_ids:
            alias_ids.append(alias)

    for alias in alias_ids:
        if alias != canonical_sandbox_id:
            await _store_sandbox_id_mapping(alias, canonical_sandbox_id)

    if matched_sandbox_id and matched_sandbox_id != canonical_sandbox_id:
        logger.warning(
            "Sandbox metadata id mismatch; using canonical metadata id",
            extra={
                "requested_sandbox_id": requested_sandbox_id,
                "resolved_sandbox_id": resolved_sandbox_id,
                "matched_sandbox_id": matched_sandbox_id,
                "canonical_sandbox_id": canonical_sandbox_id,
                "sandbox_type": sandbox_type,
            },
        )

    return {
        'project_id': project_id,
        'sandbox_id': canonical_sandbox_id,
        'sandbox_type': sandbox_type,
        'alias_ids': alias_ids,
        'sandbox_info': sandbox_info,
    }


async def _load_project_sandbox_context_by_project_id(
    client,
    project_id: str,
    *,
    fallback_sandbox_id: Optional[str] = None,
) -> Dict[str, Any]:
    project_result = await (
        client
        .table('projects')
        .select('project_id, sandbox')
        .eq('project_id', project_id)
        .execute()
    )
    if not project_result.data:
        raise HTTPException(
            status_code=404,
            detail={
                "message": "Project not found while loading sandbox metadata",
                "error_code": "PROJECT_NOT_FOUND",
                "recoverable": False,
            },
        )

    row = project_result.data[0]
    sandbox_info = _parse_sandbox_info(row.get('sandbox', {}))
    canonical_sandbox_id = str(
        sandbox_info.get('id')
        or fallback_sandbox_id
        or ''
    ).strip()
    if not canonical_sandbox_id:
        raise HTTPException(
            status_code=500,
            detail={
                "message": "Project sandbox metadata does not contain a valid sandbox id",
                "error_code": "SANDBOX_METADATA_INVALID",
                "recoverable": False,
            },
        )

    sandbox_type = str(sandbox_info.get('type', 'desktop') or 'desktop')
    alias_ids: List[str] = []
    for alias in (fallback_sandbox_id, canonical_sandbox_id):
        if alias and alias not in alias_ids:
            alias_ids.append(alias)

    for alias in alias_ids:
        if alias != canonical_sandbox_id:
            await _store_sandbox_id_mapping(alias, canonical_sandbox_id)

    return {
        'project_id': project_id,
        'sandbox_id': canonical_sandbox_id,
        'sandbox_type': sandbox_type,
        'alias_ids': alias_ids,
        'sandbox_info': sandbox_info,
    }


async def _connect_sandbox_with_retry(sandbox_id: str, sandbox_type: str):
    connect_attempts = max(1, SANDBOX_CONNECT_RETRY_COUNT + 1)
    last_error: Optional[Exception] = None

    for attempt in range(1, connect_attempts + 1):
        try:
            return await asyncio.wait_for(
                get_or_start_sandbox(sandbox_id, sandbox_type),
                timeout=SANDBOX_CONNECT_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            last_error = SandboxConnectTimeoutError(
                sandbox_id=sandbox_id,
                sandbox_type=sandbox_type,
                timeout_seconds=SANDBOX_CONNECT_TIMEOUT_SECONDS,
            )
            logger.error(str(last_error))
        except Exception as connect_error:
            last_error = connect_error
            logger.warning(
                "Sandbox connect attempt failed",
                extra={
                    'sandbox_id': sandbox_id,
                    'sandbox_type': sandbox_type,
                    'attempt': attempt,
                    'max_attempts': connect_attempts,
                    'error': str(connect_error),
                },
            )

        if attempt < connect_attempts and SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS > 0:
            await asyncio.sleep(SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS * attempt)

    if last_error:
        raise last_error
    raise RuntimeError(f"Sandbox {sandbox_id} connection failed for unknown reason")


async def _connect_if_running(sandbox_id: str, sandbox_type: str) -> Optional[Any]:
    try:
        sandbox_obj = await _connect_sandbox_with_retry(sandbox_id, sandbox_type)
    except Exception:
        return None

    resolved_id = _extract_sandbox_object_id(sandbox_obj, sandbox_id)
    if await _probe_sandbox_is_running(sandbox_obj, sandbox_id=resolved_id):
        return sandbox_obj
    return None


async def _recover_stale_sandbox_with_singleflight(
    client,
    *,
    project_id: str,
    sandbox_type: str,
    sandbox_info: Dict[str, Any],
    canonical_sandbox_id: str,
    alias_ids: List[str],
    reason: str,
) -> Any:
    import uuid as _uuid

    async with _project_recovery_singleflight(project_id):
        latest_context = await _load_project_sandbox_context_by_project_id(
            client,
            project_id,
            fallback_sandbox_id=canonical_sandbox_id,
        )
        latest_sandbox_id = latest_context['sandbox_id']
        latest_sandbox_type = latest_context['sandbox_type']
        latest_sandbox_info = latest_context['sandbox_info']
        latest_alias_ids = list(dict.fromkeys([*alias_ids, *latest_context['alias_ids']]))

        if latest_sandbox_id != canonical_sandbox_id:
            reused = await _connect_if_running(latest_sandbox_id, latest_sandbox_type)
            if reused is not None:
                for old_id in latest_alias_ids:
                    if old_id != latest_sandbox_id:
                        await _store_sandbox_id_mapping(old_id, latest_sandbox_id)
                logger.info(
                    "Sandbox recovery reused existing switched sandbox",
                    extra={
                        "project_id": project_id,
                        "reason": reason,
                        "requested_id": canonical_sandbox_id,
                        "resolved_id": latest_sandbox_id,
                    },
                )
                return reused

            canonical_sandbox_id = latest_sandbox_id
            sandbox_type = latest_sandbox_type
            sandbox_info = latest_sandbox_info
            alias_ids = latest_alias_ids

        resume_password = str(_uuid.uuid4())
        try:
            recovered = await _resume_or_create_and_finalize(
                client,
                project_id=project_id,
                sandbox_type=sandbox_type,
                sandbox_info=sandbox_info,
                canonical_sandbox_id=canonical_sandbox_id,
                alias_ids=alias_ids,
                password=resume_password,
            )
            logger.info(
                "Sandbox recovery resumed original sandbox",
                extra={
                    "project_id": project_id,
                    "reason": reason,
                    "requested_id": canonical_sandbox_id,
                    "resolved_id": _extract_sandbox_object_id(recovered, canonical_sandbox_id),
                },
            )
            return recovered
        except SandboxOriginalReuseExhausted as exhausted_error:
            logger.warning(
                "Original sandbox recovery exhausted; recreating same-type sandbox",
                extra={
                    "project_id": project_id,
                    "reason": reason,
                    "requested_id": canonical_sandbox_id,
                    "sandbox_type": sandbox_type,
                    "error": str(exhausted_error.last_error or exhausted_error),
                },
            )

        recreated, recreated_id = await _recreate_project_sandbox(
            client,
            project_id=project_id,
            sandbox_type=sandbox_type,
            alias_ids=alias_ids,
        )
        logger.info(
            "Sandbox recovery recreated sandbox after original recovery exhaustion",
            extra={
                "project_id": project_id,
                "reason": reason,
                "requested_id": canonical_sandbox_id,
                "resolved_id": recreated_id,
                "sandbox_type": sandbox_type,
            },
        )
        return recreated


async def _recreate_project_sandbox(
    client,
    *,
    project_id: str,
    sandbox_type: str,
    alias_ids: List[str],
):
    import uuid

    sandbox_pass = str(uuid.uuid4())
    new_sandbox = await asyncio.wait_for(
        create_sandbox(sandbox_pass, project_id, sandbox_type),
        timeout=SANDBOX_CONNECT_TIMEOUT_SECONDS,
    )
    new_sandbox_id = (
        getattr(new_sandbox, 'sandbox_id', None)
        or getattr(new_sandbox, 'id', None)
    )
    if not new_sandbox_id:
        raise RuntimeError('Created sandbox has no id')

    vnc_url = None
    website_url = None
    if sandbox_type == 'desktop':
        try:
            await _run_blocking_sandbox_call(
                'stream.start',
                lambda: new_sandbox.stream.start(),
                timeout_seconds=SANDBOX_IO_TIMEOUT_SECONDS,
                sandbox_id=str(new_sandbox_id),
            )
            vnc_url_raw = await _run_blocking_sandbox_call(
                'stream.get_url',
                lambda: new_sandbox.stream.get_url(),
                timeout_seconds=SANDBOX_IO_TIMEOUT_SECONDS,
                sandbox_id=str(new_sandbox_id),
            )
            if isinstance(vnc_url_raw, list):
                vnc_url = vnc_url_raw[0] if vnc_url_raw else ''
            elif hasattr(vnc_url_raw, 'url'):
                vnc_url = vnc_url_raw.url
            else:
                vnc_url = str(vnc_url_raw) if vnc_url_raw else ''
            website_url = vnc_url
        except Exception as stream_error:
            logger.warning(f"Desktop stream setup failed (non-fatal): {stream_error}")

    sandbox_metadata = {
        'id': str(new_sandbox_id),
        'pass': sandbox_pass,
        'type': sandbox_type,
        'vnc_preview': vnc_url if isinstance(vnc_url, str) else None,
        'sandbox_url': website_url if isinstance(website_url, str) else None,
        'token': None,
        'state': 'running',
        'created_at': datetime.now(timezone.utc).isoformat(),
        'last_accessed_at': datetime.now(timezone.utc).isoformat(),
        'pause_grace_seconds': _get_auto_pause_grace_seconds(),
        'lifecycle_started_at': datetime.now(timezone.utc).isoformat(),
        'preclone_status': 'pending',
        'preclone_fail_count': 0,
        'preclone_due_at': _compute_preclone_due_at(
            datetime.now(timezone.utc),
            _get_preclone_threshold_seconds(),
        ),
    }

    await client.table('projects').eq('project_id', project_id).update({
        'sandbox': json.dumps(sandbox_metadata)
    })

    ids_to_map = list(dict.fromkeys([*alias_ids, str(new_sandbox_id)]))
    for old_id in ids_to_map:
        await _store_sandbox_id_mapping(old_id, str(new_sandbox_id))

    logger.info(
        "Sandbox recreated successfully",
        extra={
            'project_id': project_id,
            'sandbox_type': sandbox_type,
            'new_sandbox_id': str(new_sandbox_id),
        },
    )
    try:
        await _rehydrate_workspace_artifacts_into_sandbox(
            client,
            project_id=project_id,
            sandbox_obj=new_sandbox,
            sandbox_id=str(new_sandbox_id),
        )
    except Exception as rehydrate_error:
        logger.warning(
            "Workspace artifact rehydrate failed for project=%s sandbox=%s: %s",
            project_id,
            new_sandbox_id,
            rehydrate_error,
        )
    return new_sandbox, str(new_sandbox_id)


async def _refresh_shadow_clone_lease_from_project(
    client,
    *,
    lease: Dict[str, Any],
) -> Dict[str, Any]:
    project_context = await _load_project_sandbox_context_by_project_id(
        client,
        lease["project_id"],
        fallback_sandbox_id=lease["sandbox_id"],
    )
    return await save_run_sandbox_lease(
        lease["run_id"],
        project_id=lease["project_id"],
        thread_id=lease.get("thread_id") or "",
        sandbox_id=lease["sandbox_id"],
        sandbox_type=str(
            project_context["sandbox_info"].get("type")
            or lease.get("sandbox_type")
            or "desktop"
        ).strip()
        or "desktop",
        sandbox_info=project_context["sandbox_info"],
        binding_state="locked",
        source=lease.get("source") or "strict_shadow_clone",
        last_error=None,
    )


async def create_shadow_clone_standby_sandbox(
    client,
    *,
    lease: Dict[str, Any],
    layer_index: int,
    completed_subtask_ids: List[str],
) -> Dict[str, Any]:
    sandbox_id = str(lease.get("sandbox_id") or "").strip()
    sandbox_type = str(lease.get("sandbox_type") or "desktop").strip() or "desktop"
    if not sandbox_id:
        raise RuntimeError("Shadow Clone standby clone cannot be created without an active sandbox id")

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    clone_timeout_seconds = _get_preclone_clone_timeout_seconds()
    new_sandbox_id, snapshot_template_id = await clone_sandbox(
        sandbox_id,
        count=1,
        timeout=clone_timeout_seconds,
    )
    try:
        await extend_sandbox_timeout(
            str(new_sandbox_id),
            sandbox_type,
            timeout_window_seconds=clone_timeout_seconds,
            connect_timeout_seconds=SANDBOX_CONNECT_TIMEOUT_SECONDS,
            require_running=True,
        )
    except Exception as validation_error:
        try:
            await delete_sandbox(str(new_sandbox_id))
        except Exception as cleanup_error:
            logger.warning(
                "Failed to clean up invalid standby clone sandbox %s: %s",
                new_sandbox_id,
                cleanup_error,
            )
        raise RuntimeError(
            f"Shadow Clone standby sandbox {new_sandbox_id} failed validation: "
            f"{validation_error}"
        ) from validation_error

    active_info = _build_shadow_clone_bound_sandbox_info(lease)
    standby_info = {
        **active_info,
        "id": str(new_sandbox_id),
        "type": sandbox_type,
        "state": "running",
        "created_at": now_iso,
        "last_clone_at": now_iso,
        "clone_parent_id": sandbox_id,
        "snapshot_template_id": snapshot_template_id,
        "lifecycle_started_at": now_iso,
        "preclone_status": "ok",
        "preclone_fail_count": 0,
        "preclone_due_at": _compute_preclone_due_at(now, _get_preclone_threshold_seconds()),
        "shadow_clone_checkpoint_layer_index": int(layer_index),
        "shadow_clone_checkpoint_subtask_ids": list(completed_subtask_ids),
    }
    standby_info.pop("paused_at", None)

    previous_standby_id = str(lease.get("standby_sandbox_id") or "").strip()
    if previous_standby_id and previous_standby_id != str(new_sandbox_id):
        try:
            await delete_sandbox(previous_standby_id)
        except Exception as cleanup_error:
            logger.warning(
                "Failed to delete superseded standby clone sandbox %s: %s",
                previous_standby_id,
                cleanup_error,
            )

    return {
        "standby_sandbox_id": str(new_sandbox_id),
        "standby_sandbox_type": sandbox_type,
        "standby_sandbox_info": standby_info,
        "standby_snapshot_template_id": snapshot_template_id,
        "standby_source_sandbox_id": sandbox_id,
        "standby_created_at": now_iso,
        "approved_sandbox_ids": list(
            dict.fromkeys([sandbox_id, str(new_sandbox_id)])
        ),
    }


async def promote_shadow_clone_standby_sandbox(
    client,
    *,
    lease: Dict[str, Any],
) -> Dict[str, Any]:
    run_id = str(lease.get("run_id") or "").strip()
    project_id = str(lease.get("project_id") or "").strip()
    active_sandbox_id = str(lease.get("sandbox_id") or "").strip()
    standby_sandbox_id = str(lease.get("standby_sandbox_id") or "").strip()
    standby_sandbox_type = str(
        lease.get("standby_sandbox_type")
        or lease.get("sandbox_type")
        or "desktop"
    ).strip() or "desktop"
    standby_sandbox_info = _parse_sandbox_info(lease.get("standby_sandbox_info"))
    if not run_id or not project_id or not standby_sandbox_id:
        raise RuntimeError("Shadow Clone standby promotion requested without a standby sandbox")

    connect_lock = await _get_sandbox_connect_lock(standby_sandbox_id)
    async with connect_lock:
        sandbox_obj = await _connect_if_running(standby_sandbox_id, standby_sandbox_type)
        if sandbox_obj is None:
            recovery_password = str(
                standby_sandbox_info.get("pass")
                or lease.get("pass")
                or lease.get("sandbox_info", {}).get("pass")
                or ""
            ).strip()
            if not recovery_password:
                import uuid as _uuid

                recovery_password = str(_uuid.uuid4())
            sandbox_obj = await _resume_or_create_and_finalize(
                client,
                project_id=project_id,
                sandbox_type=standby_sandbox_type,
                sandbox_info={
                    **standby_sandbox_info,
                    "id": standby_sandbox_id,
                    "type": standby_sandbox_type,
                },
                canonical_sandbox_id=standby_sandbox_id,
                alias_ids=[standby_sandbox_id],
                password=recovery_password,
            )
        else:
            await _finalize_sandbox_reactivation(
                client,
                sandbox_obj,
                sandbox_info={
                    **standby_sandbox_info,
                    "id": standby_sandbox_id,
                    "type": standby_sandbox_type,
                },
                sandbox_type=standby_sandbox_type,
                project_id=project_id,
                canonical_sandbox_id=standby_sandbox_id,
                alias_ids=[standby_sandbox_id],
                action="rebound",
            )

    if active_sandbox_id and active_sandbox_id != standby_sandbox_id:
        await _store_sandbox_id_mapping(active_sandbox_id, standby_sandbox_id)

    project_context = await _load_project_sandbox_context_by_project_id(
        client,
        project_id,
        fallback_sandbox_id=standby_sandbox_id,
    )
    updated_lease = await update_run_sandbox_lease(
        run_id,
        sandbox_id=standby_sandbox_id,
        sandbox_type=standby_sandbox_type,
        sandbox_info=project_context["sandbox_info"],
        binding_state="locked",
        standby_sandbox_id=None,
        standby_sandbox_type=None,
        standby_sandbox_info={},
        standby_snapshot_template_id=None,
        standby_source_sandbox_id=None,
        standby_created_at=None,
        approved_sandbox_ids=[standby_sandbox_id],
        last_error=None,
    )
    return updated_lease or lease


async def attach_shadow_clone_bound_sandbox(
    client,
    *,
    lease: Dict[str, Any],
) -> Any:
    """Attach or reattach the exact sandbox bound to a Shadow Clone run.

    This path never recreates or switches to another sandbox.
    """
    run_id = str(lease.get("run_id") or "").strip()
    project_id = str(lease.get("project_id") or "").strip()
    sandbox_id = str(lease.get("sandbox_id") or "").strip()
    if not run_id or not project_id or not sandbox_id:
        raise ShadowCloneStrictSandboxError(
            run_id=run_id or "<missing>",
            sandbox_id=sandbox_id or "<missing>",
            project_id=project_id or "<missing>",
            detail="Shadow Clone sandbox lease is incomplete; execution cannot continue safely.",
            error_code="SHADOW_CLONE_SANDBOX_LEASE_INVALID",
            recoverable=False,
            root_cause_code="SHADOW_CLONE_SANDBOX_LEASE_INVALID",
            recovery_action="replan",
        )

    ensure_shadow_clone_attachable_lease(
        lease,
        run_id=run_id,
        project_id=project_id,
        sandbox_id=sandbox_id,
    )
    sandbox_info = _build_shadow_clone_bound_sandbox_info(lease)
    sandbox_type = str(sandbox_info.get("type") or "desktop").strip() or "desktop"
    connect_lock = await _get_sandbox_connect_lock(sandbox_id)

    def _standby_clone_available(candidate_lease: Dict[str, Any]) -> bool:
        if not is_shadow_clone_proactive_standby_enabled():
            return False
        return bool(str((candidate_lease or {}).get("standby_sandbox_id") or "").strip())

    async with connect_lock:
        current_lease = await get_run_sandbox_lease(run_id)
        if current_lease:
            lease = current_lease
            sandbox_id = str(lease.get("sandbox_id") or sandbox_id).strip() or sandbox_id
            sandbox_info = _build_shadow_clone_bound_sandbox_info(lease)
            sandbox_type = str(sandbox_info.get("type") or sandbox_type).strip() or sandbox_type
        ensure_shadow_clone_attachable_lease(
            lease,
            run_id=run_id,
            project_id=project_id,
            sandbox_id=sandbox_id,
        )

        try:
            sandbox_obj = await _connect_sandbox_with_retry(sandbox_id, sandbox_type)
            connected_id = _extract_sandbox_object_id(sandbox_obj, sandbox_id)
            if connected_id != sandbox_id:
                raise ShadowCloneStrictSandboxError(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    project_id=project_id,
                    detail=(
                        f"Shadow Clone run {run_id} is bound to sandbox {sandbox_id}, "
                        f"but attach returned {connected_id}. Refusing to switch sandboxes."
                    ),
                    error_code="SHADOW_CLONE_SANDBOX_POINTER_DRIFT_BLOCKED",
                    recoverable=False,
                    binding_state=_normalize_shadow_clone_binding_state(lease.get("binding_state")),
                    root_cause_code="SHADOW_CLONE_SANDBOX_POINTER_DRIFT",
                    recovery_action="replan",
                )
            if await _probe_sandbox_is_running(sandbox_obj, sandbox_id=connected_id):
                await set_run_sandbox_binding_state(run_id, "locked", last_error=None)
                return sandbox_obj
        except ShadowCloneStrictSandboxError:
            raise
        except Exception as connect_error:
            logger.warning(
                "Shadow Clone strict attach failed; attempting same-sandbox reattach only",
                extra={
                    "run_id": run_id,
                    "project_id": project_id,
                    "sandbox_id": sandbox_id,
                    "sandbox_type": sandbox_type,
                    "error": str(connect_error),
                },
            )

        await set_run_sandbox_binding_state(
            run_id,
            "reattaching",
            last_error=f"strict attach failed for sandbox {sandbox_id}",
        )

        try:
            async with _project_recovery_singleflight(project_id):
                current_lease = await get_run_sandbox_lease(run_id)
                if current_lease:
                    lease = current_lease
                    sandbox_id = str(lease.get("sandbox_id") or sandbox_id).strip() or sandbox_id
                    sandbox_info = _build_shadow_clone_bound_sandbox_info(lease)
                    sandbox_type = (
                        str(sandbox_info.get("type") or sandbox_type).strip() or sandbox_type
                    )
                ensure_shadow_clone_attachable_lease(
                    lease,
                    run_id=run_id,
                    project_id=project_id,
                    sandbox_id=sandbox_id,
                )

                recovery_password = str(sandbox_info.get("pass") or "").strip()
                if not recovery_password:
                    import uuid as _uuid

                    recovery_password = str(_uuid.uuid4())

                sandbox_obj, action = await resume_or_create_sandbox(
                    password=recovery_password,
                    project_id=project_id,
                    sandbox_type=sandbox_type,
                    sandbox_info=sandbox_info,
                    reattach_budget_seconds=_get_reattach_budget_seconds(),
                    allow_create=False,
                )
                resolved_id = _extract_sandbox_object_id(sandbox_obj, sandbox_id)
                if resolved_id != sandbox_id:
                    raise ShadowCloneStrictSandboxError(
                        run_id=run_id,
                        sandbox_id=sandbox_id,
                        project_id=project_id,
                        detail=(
                            f"Shadow Clone run {run_id} is bound to sandbox {sandbox_id}, "
                            f"but recovery returned {resolved_id}. Refusing sandbox failover."
                        ),
                        error_code="SHADOW_CLONE_SANDBOX_RECREATE_FORBIDDEN",
                        recoverable=False,
                        binding_state=_normalize_shadow_clone_binding_state(lease.get("binding_state")),
                        root_cause_code="SHADOW_CLONE_SANDBOX_POINTER_DRIFT",
                        recovery_action="replan",
                    )
                if not await _probe_sandbox_is_running(sandbox_obj, sandbox_id=resolved_id):
                    raise SandboxOriginalReuseExhausted(
                        sandbox_id,
                        sandbox_type,
                        project_id,
                        last_error=RuntimeError(
                            f"Shadow Clone strict recovery returned non-running sandbox {resolved_id}"
                        ),
                    )

                await _finalize_sandbox_reactivation(
                    client,
                    sandbox_obj,
                    sandbox_info=sandbox_info,
                    sandbox_type=sandbox_type,
                    project_id=project_id,
                    canonical_sandbox_id=sandbox_id,
                    alias_ids=[sandbox_id],
                    action=action,
                    password=recovery_password,
                )
        except SandboxOriginalReuseExhausted as recover_error:
            latest_lease = await _clear_disabled_shadow_clone_standby_metadata(
                run_id,
                await get_run_sandbox_lease(run_id) or lease,
            )
            root_cause_metadata = _extract_shadow_clone_root_cause_metadata(
                recover_error.last_error or recover_error,
            )
            if _standby_clone_available(latest_lease):
                await update_run_sandbox_lease(
                    run_id,
                    binding_state="recovery_required",
                    last_error=str(recover_error.last_error or recover_error),
                )
                raise ShadowCloneStrictSandboxError(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    project_id=project_id,
                    detail=(
                        f"Shadow Clone sandbox {sandbox_id} could not be reattached for run {run_id}. "
                        "A run-approved standby clone is available, so execution must restart the "
                        "interrupted layer from the last completed checkpoint."
                    ),
                    error_code="SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
                    recoverable=True,
                    binding_state="recovery_required",
                    root_cause_code=root_cause_metadata.get("root_cause_code"),
                    recovery_action="clone_recovery",
                    provider_reason=root_cause_metadata.get("provider_reason"),
                    provider_status_code=root_cause_metadata.get("provider_status_code"),
                    provider_code=root_cause_metadata.get("provider_code"),
                    provider_retryable=root_cause_metadata.get("provider_retryable"),
                ) from recover_error
            await set_run_sandbox_binding_state(
                run_id,
                "lost",
                last_error=str(recover_error.last_error or recover_error),
            )
            raise ShadowCloneStrictSandboxError(
                run_id=run_id,
                sandbox_id=sandbox_id,
                project_id=project_id,
                detail=(
                    f"Shadow Clone sandbox {sandbox_id} could not be reattached for run {run_id}. "
                    "Strict single-sandbox mode forbids creating a replacement sandbox."
                ),
                error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
                recoverable=False,
                binding_state="lost",
                root_cause_code=root_cause_metadata.get("root_cause_code"),
                recovery_action="replan",
                provider_reason=root_cause_metadata.get("provider_reason"),
                provider_status_code=root_cause_metadata.get("provider_status_code"),
                provider_code=root_cause_metadata.get("provider_code"),
                provider_retryable=root_cause_metadata.get("provider_retryable"),
            ) from recover_error
        except ShadowCloneStrictSandboxError as strict_error:
            latest_lease = await _clear_disabled_shadow_clone_standby_metadata(
                run_id,
                await get_run_sandbox_lease(run_id) or lease,
            )
            if _standby_clone_available(latest_lease):
                await update_run_sandbox_lease(
                    run_id,
                    binding_state="recovery_required",
                    last_error=str(strict_error),
                )
                raise ShadowCloneStrictSandboxError(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    project_id=project_id,
                    detail=(
                        f"Shadow Clone sandbox {sandbox_id} hit strict pointer or recovery drift for run {run_id}. "
                        "A run-approved standby clone is available and must be promoted instead of "
                        "switching this subagent in-place."
                    ),
                    error_code="SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
                    recoverable=True,
                    binding_state="recovery_required",
                    root_cause_code=(
                        strict_error.root_cause_code
                        or "SHADOW_CLONE_SANDBOX_POINTER_DRIFT"
                    ),
                    recovery_action="clone_recovery",
                    provider_reason=strict_error.provider_reason,
                    provider_status_code=strict_error.provider_status_code,
                    provider_code=strict_error.provider_code,
                    provider_retryable=strict_error.provider_retryable,
                ) from strict_error
            await set_run_sandbox_binding_state(
                run_id,
                "lost",
                last_error="strict sandbox mismatch detected",
            )
            raise
        except Exception as recover_error:
            latest_lease = await _clear_disabled_shadow_clone_standby_metadata(
                run_id,
                await get_run_sandbox_lease(run_id) or lease,
            )
            root_cause_metadata = _extract_shadow_clone_root_cause_metadata(recover_error)
            if _standby_clone_available(latest_lease):
                await update_run_sandbox_lease(
                    run_id,
                    binding_state="recovery_required",
                    last_error=str(recover_error),
                )
                raise ShadowCloneStrictSandboxError(
                    run_id=run_id,
                    sandbox_id=sandbox_id,
                    project_id=project_id,
                    detail=(
                        f"Shadow Clone sandbox {sandbox_id} failed strict reattach for run {run_id}: "
                        f"{recover_error}. A run-approved standby clone is available for checkpoint recovery."
                    ),
                    error_code="SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
                    recoverable=True,
                    binding_state="recovery_required",
                    root_cause_code=root_cause_metadata.get("root_cause_code"),
                    recovery_action="clone_recovery",
                    provider_reason=root_cause_metadata.get("provider_reason"),
                    provider_status_code=root_cause_metadata.get("provider_status_code"),
                    provider_code=root_cause_metadata.get("provider_code"),
                    provider_retryable=root_cause_metadata.get("provider_retryable"),
                ) from recover_error
            await set_run_sandbox_binding_state(
                run_id,
                "lost",
                last_error=str(recover_error),
            )
            raise ShadowCloneStrictSandboxError(
                run_id=run_id,
                sandbox_id=sandbox_id,
                project_id=project_id,
                detail=(
                    f"Shadow Clone sandbox {sandbox_id} failed strict reattach for run {run_id}: "
                    f"{recover_error}"
                ),
                error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
                recoverable=False,
                binding_state="lost",
                root_cause_code=root_cause_metadata.get("root_cause_code"),
                recovery_action="replan",
                provider_reason=root_cause_metadata.get("provider_reason"),
                provider_status_code=root_cause_metadata.get("provider_status_code"),
                provider_code=root_cause_metadata.get("provider_code"),
                provider_retryable=root_cause_metadata.get("provider_retryable"),
            ) from recover_error

        await _refresh_shadow_clone_lease_from_project(
            client,
            lease=lease,
        )
        await set_run_sandbox_binding_state(run_id, "locked", last_error=None)
        logger.info(
            "Shadow Clone strict sandbox reattached successfully",
            extra={
                "run_id": run_id,
                "project_id": project_id,
                "sandbox_id": sandbox_id,
                "sandbox_type": sandbox_type,
            },
        )
        return sandbox_obj


async def _get_project_sandbox_for_request(
    client,
    *,
    project_id: str,
    requested_sandbox_id: str,
    include_recent_shadow_clone_lease: bool = False,
):
    preferred_lease = await get_preferred_project_sandbox_lease(
        project_id,
        requested_sandbox_id=requested_sandbox_id,
        include_recent=include_recent_shadow_clone_lease,
    )
    if preferred_lease is not None:
        sandbox_obj = await attach_shadow_clone_bound_sandbox(
            client,
            lease=preferred_lease,
        )
        return sandbox_obj, _extract_sandbox_object_id(
            sandbox_obj,
            str(preferred_lease.get("sandbox_id") or requested_sandbox_id),
        ), preferred_lease

    sandbox_obj = await get_sandbox_by_id_safely(
        client,
        requested_sandbox_id,
        project_id=project_id,
    )
    return sandbox_obj, _extract_sandbox_object_id(sandbox_obj, requested_sandbox_id), None


async def get_sandbox_by_id_safely(client, sandbox_id: str, *, project_id: Optional[str] = None):
    """
    Safely retrieve a sandbox object by its ID, using the project that owns it.
    If the sandbox is paused, transparently resume it before connecting.
    If sandbox attach fails, the API will attempt controlled self-healing by recreating it.
    """
    if project_id:
        context = await _load_project_sandbox_context_by_project_id(
            client,
            project_id,
            fallback_sandbox_id=sandbox_id,
        )
    else:
        context = await _load_project_sandbox_context(client, sandbox_id)

    project_id = context['project_id']
    canonical_sandbox_id = context['sandbox_id']
    sandbox_type = context['sandbox_type']
    alias_ids = context['alias_ids']
    sandbox_info = context['sandbox_info']

    connect_lock = await _get_sandbox_connect_lock(canonical_sandbox_id)

    async with connect_lock:
        # Re-read project sandbox metadata inside the connect lock to avoid
        # queued requests operating on stale pre-switch IDs.
        refreshed_context = await _load_project_sandbox_context_by_project_id(
            client,
            project_id,
            fallback_sandbox_id=canonical_sandbox_id,
        )
        canonical_sandbox_id = refreshed_context['sandbox_id']
        sandbox_type = refreshed_context['sandbox_type']
        alias_ids = list(dict.fromkeys([*alias_ids, *refreshed_context['alias_ids']]))
        sandbox_info = refreshed_context['sandbox_info']

        preclone_context = await _maybe_preclone_sandbox_context(
            client,
            project_id=project_id,
            sandbox_info=sandbox_info,
            sandbox_type=sandbox_type,
            canonical_sandbox_id=canonical_sandbox_id,
            alias_ids=alias_ids,
        )
        sandbox_info = preclone_context['sandbox_info']
        canonical_sandbox_id = preclone_context['sandbox_id']
        alias_ids = preclone_context['alias_ids']
        was_paused = sandbox_info.get('state') == 'paused'

        # ── Paused sandbox: resume (or recreate) before normal connect ──
        if was_paused:
            try:
                return await _recover_stale_sandbox_with_singleflight(
                    client,
                    project_id=project_id,
                    sandbox_type=sandbox_type,
                    sandbox_info=sandbox_info,
                    canonical_sandbox_id=canonical_sandbox_id,
                    alias_ids=alias_ids,
                    reason='paused_state',
                )
            except Exception as resume_err:
                logger.warning(
                    "Paused sandbox resume failed; falling through to normal connect/recovery",
                    extra={
                        'sandbox_id': canonical_sandbox_id,
                        'project_id': project_id,
                        'error': str(resume_err),
                    },
                )

        # ── Normal connect path ──
        try:
            sandbox_obj = await _connect_sandbox_with_retry(canonical_sandbox_id, sandbox_type)
            connected_id = _extract_sandbox_object_id(sandbox_obj, canonical_sandbox_id)
            alive = await _probe_sandbox_is_running(
                sandbox_obj,
                sandbox_id=connected_id,
            )
            if alive:
                return sandbox_obj

            logger.warning(
                "Sandbox connected but is_running() returned False; resuming/recreating",
                extra={
                    'sandbox_id': canonical_sandbox_id,
                    'project_id': project_id,
                    'sandbox_type': sandbox_type,
                },
            )
            return await _recover_stale_sandbox_with_singleflight(
                client,
                project_id=project_id,
                sandbox_type=sandbox_type,
                sandbox_info=sandbox_info,
                canonical_sandbox_id=canonical_sandbox_id,
                alias_ids=alias_ids,
                reason='probe_false',
            )
        except Exception as connect_error:
            logger.warning(
                "Sandbox attach failed; entering recovery path",
                extra={
                    'sandbox_id': canonical_sandbox_id,
                    'sandbox_type': sandbox_type,
                    'project_id': project_id,
                    'error': str(connect_error),
                },
            )
            if SANDBOX_RECOVERY_MAX_ATTEMPTS <= 0:
                raise _sandbox_error_to_http(connect_error, action='connect sandbox')

        if not project_id:
            raise HTTPException(
                status_code=500,
                detail={
                    "message": f"Failed to retrieve sandbox: {canonical_sandbox_id}",
                    "error_code": "SANDBOX_PROJECT_LOOKUP_FAILED",
                    "recoverable": False,
                },
            )

        last_recreate_error: Optional[Exception] = None
        for attempt in range(1, SANDBOX_RECOVERY_MAX_ATTEMPTS + 1):
            try:
                logger.info(
                    "Sandbox recovery retry after attach failure",
                    extra={
                        'sandbox_id': canonical_sandbox_id,
                        'project_id': project_id,
                        'sandbox_type': sandbox_type,
                        'attempt': attempt,
                        'max_attempts': SANDBOX_RECOVERY_MAX_ATTEMPTS,
                    },
                )
                return await _recover_stale_sandbox_with_singleflight(
                    client,
                    project_id=project_id,
                    sandbox_type=sandbox_type,
                    sandbox_info=sandbox_info,
                    canonical_sandbox_id=canonical_sandbox_id,
                    alias_ids=alias_ids,
                    reason=f'connect_error_attempt_{attempt}',
                )
            except Exception as recover_error:
                last_recreate_error = recover_error
                logger.error(
                    "Sandbox recreation attempt failed",
                    extra={
                        'sandbox_id': canonical_sandbox_id,
                        'project_id': project_id,
                        'sandbox_type': sandbox_type,
                        'attempt': attempt,
                        'max_attempts': SANDBOX_RECOVERY_MAX_ATTEMPTS,
                        'error': str(recover_error),
                    },
                )

        if isinstance(last_recreate_error, asyncio.TimeoutError):
            raise HTTPException(
                status_code=504,
                detail={
                    "message": (
                        f"Sandbox recovery recreate phase timed out after "
                        f"{SANDBOX_CONNECT_TIMEOUT_SECONDS:.1f}s"
                    ),
                    "error_code": "SANDBOX_RECOVERY_TIMEOUT",
                    "recoverable": True,
                    "resolved_sandbox_id": canonical_sandbox_id,
                },
            )

        if isinstance(last_recreate_error, HTTPException):
            raise last_recreate_error

        raise HTTPException(
            status_code=500,
            detail={
                "message": (
                    f"Sandbox {canonical_sandbox_id} attach failed and recovery exhausted: "
                    f"{str(last_recreate_error) if last_recreate_error else 'unknown error'}"
                ),
                "error_code": "SANDBOX_RECOVERY_FAILED",
                "recoverable": False,
                "resolved_sandbox_id": canonical_sandbox_id,
            },
        )

@router.post("/sandboxes/{sandbox_id}/files")
async def create_file(
    sandbox_id: str, 
    path: str = Form(...),
    file: UploadFile = File(...),
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Create a file in the sandbox using direct file upload"""
    original_sandbox_id = sandbox_id
    sandbox_id = await _resolve_sandbox_id(sandbox_id)
    # Normalize the path to handle UTF-8 encoding correctly
    path = normalize_path(path)

    logger.info(f"Received file upload request for sandbox {sandbox_id}, path: {path}, user_id: {user_id}")
    client = await db.client

    # Verify the user has access to this sandbox
    project_data = await verify_sandbox_access(
        client,
        sandbox_id,
        user_id,
        original_sandbox_id=original_sandbox_id,
    )
    project_id = project_data.get('project_id')
    project_sandbox_info = _parse_sandbox_info(project_data.get('sandbox', {}))

    content = await file.read()
    sandbox_written = False
    active_sandbox_id = sandbox_id

    try:
        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=sandbox_id,
        )

        sandbox, active_sandbox_id, _ = await _get_project_sandbox_for_request(
            client,
            project_id=project_id,
            requested_sandbox_id=sandbox_id,
            include_recent_shadow_clone_lease=True,
        )
        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=active_sandbox_id,
        )

        await _run_guarded_sandbox_io(
            active_sandbox_id,
            'files.write',
            lambda: sandbox.files.write(path, content),
            path=path,
        )
        sandbox_written = True
        logger.info(f"File created at {path} in sandbox {active_sandbox_id}")
    except ShadowCloneStrictSandboxError as strict_error:
        if not path.startswith("/workspace"):
            raise _shadow_clone_strict_error_to_http(strict_error)
        logger.warning(
            "Sandbox unavailable for file upload due to strict single-sandbox enforcement; persisted durable artifact only: %s",
            strict_error,
        )
    except HTTPException as http_error:
        if not path.startswith("/workspace"):
            raise http_error
        logger.warning(
            "Sandbox unavailable for file upload due to HTTP error; persisted durable artifact only: %s",
            getattr(http_error, "detail", http_error),
        )
    except Exception as sandbox_error:
        if not path.startswith("/workspace"):
            raise _sandbox_error_to_http(sandbox_error, action='create file')
        logger.warning(
            "Sandbox unavailable for file upload; persisted durable artifact only: %s",
            sandbox_error,
        )

    if path.startswith("/workspace"):
        hint_project_sandbox_id = str(
            project_sandbox_info.get("id")
            or active_sandbox_id
            or sandbox_id
            or original_sandbox_id
            or ""
        ).strip()
        try:
            await workspace_artifacts.persist_artifact(
                project_id=project_id,
                path=path,
                content=content,
                source="sandbox_api.create_file",
                client=client,
                sandbox_id=active_sandbox_id if sandbox_written else None,
                created_by_user_id=user_id,
                content_type=getattr(file, "content_type", None),
                metadata={
                    "filename": getattr(file, "filename", None),
                    "upload_origin": "sandbox_api",
                    "sandbox_written": sandbox_written,
                    "requested_sandbox_id": str(original_sandbox_id or "").strip() or None,
                    "resolved_sandbox_id": str(sandbox_id or "").strip() or None,
                    "project_sandbox_id": hint_project_sandbox_id or None,
                },
            )
        except Exception as persist_error:
            logger.warning(
                "Failed to persist workspace artifact for project=%s path=%s: %s",
                project_id,
                path,
                persist_error,
            )

    return {
        "status": "success",
        "created": True,
        "path": path,
        "sandbox_written": sandbox_written,
    }

@router.get("/sandboxes/{sandbox_data:path}/files")
async def list_files_with_json_compatibility(
    sandbox_data: str,
    path: str,
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt)
):
    """
    处理前端传来的沙箱文件列表请求 - 兼容JSON对象路径格式
    支持两种格式:
    1. 正常格式: /api/sandboxes/{sandbox_id}/files
    2. JSON格式: /api/sandboxes/{"id":"xxx",...}/files
    """
    # 尝试解析为JSON对象（前端错误格式）
    sandbox_id = None
    try:
        # 检查是否是JSON格式
        if sandbox_data.startswith('{') and sandbox_data.endswith('}'):
            logger.info(f"🔧 检测到JSON格式的sandbox参数（文件列表），正在解析...")
            sandbox_obj = json.loads(sandbox_data)
            sandbox_id = sandbox_obj.get('id')
            logger.info(f"✅ 成功从JSON中提取sandbox_id: {sandbox_id}")
        else:
            # 普通的sandbox_id格式
            sandbox_id = sandbox_data
            logger.info(f"📝 使用普通格式的sandbox_id: {sandbox_id}")
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON解析失败: {e}")
        # 降级到普通字符串处理
        sandbox_id = sandbox_data
    except Exception as e:
        logger.error(f"❌ 处理sandbox参数时出错: {e}")
        raise HTTPException(status_code=400, detail="Invalid sandbox parameter format")
    
    if not sandbox_id:
        raise HTTPException(status_code=400, detail="Sandbox ID not found in parameter")

    original_sandbox_id = sandbox_id
    sandbox_id = await _resolve_sandbox_id(sandbox_id)

    # 调用原有的文件列表逻辑
    return await _list_files_internal(sandbox_id, path, request, user_id, original_sandbox_id=original_sandbox_id)


async def _list_files_internal(
    sandbox_id: str,
    path: str,
    request: Request = None,
    user_id: str = None,
    original_sandbox_id: str = None,
):
    """List files and directories at the specified path"""
    started_at = time.monotonic()
    path = normalize_path(path)

    if _is_hidden_workspace_path(path):
        return _build_list_files_response(
            files=[],
            identity_source="hidden_path_filter",
            sandbox_available=False,
            response_source="hidden_filtered",
            sandbox_file_count=0,
            artifact_file_count=0,
        )

    logger.info(f"Received list files request for sandbox {sandbox_id}, path: {path}, user_id: {user_id}")
    client = await db.client
    include_hidden = False
    project_id: Optional[str] = None
    active_sandbox_id = sandbox_id
    sandbox_available = False
    identity_source = "unresolved"
    project_sandbox_info: Dict[str, Any] = {}
    requested_project_sandbox_id = sandbox_id
    artifact_agent_run_id = _parse_claude_local_agent_run_id(sandbox_id, original_sandbox_id)
    artifact_scope = _artifact_scope_for_agent_run(artifact_agent_run_id)
    thread_workspace_thread_id = _parse_thread_workspace_thread_id(sandbox_id, original_sandbox_id)
    try:
        if path.startswith("/workspace") and thread_workspace_thread_id:
            project_id, identity_source = await _resolve_thread_workspace_context(
                client,
                thread_id=thread_workspace_thread_id,
                user_id=user_id,
            )
            artifact_entries = await _list_thread_workspace_artifact_entries_for_path(
                client,
                project_id=project_id,
                thread_id=thread_workspace_thread_id,
                path=path,
                include_hidden=include_hidden,
            )
            if not artifact_entries:
                raise _thread_workspace_not_found_http_exception(
                    path=path,
                    identity_source=identity_source,
                    thread_id=thread_workspace_thread_id,
                )
            return _build_list_files_response(
                files=artifact_entries,
                identity_source=identity_source,
                sandbox_available=False,
                response_source="thread_workspace",
                sandbox_file_count=0,
                artifact_file_count=len(artifact_entries),
                artifact_scope="thread",
                thread_id=thread_workspace_thread_id,
            )

        delivery_context = await resolve_file_delivery_context(
            client=client,
            requested_sandbox_id=sandbox_id,
            resolved_sandbox_id=sandbox_id,
            original_sandbox_id=original_sandbox_id,
            user_id=user_id,
            verify_sandbox_access=verify_sandbox_access,
            preferred_lease_resolver=get_preferred_project_sandbox_lease,
        )
        project_id = delivery_context.project_id
        project_sandbox_info = delivery_context.project_sandbox_info
        requested_project_sandbox_id = delivery_context.project_sandbox_id or sandbox_id
        active_sandbox_id = requested_project_sandbox_id
        identity_source = delivery_context.identity_source
        shadow_clone_binding_state = delivery_context.shadow_clone_binding_state

        if path.startswith("/workspace") and artifact_agent_run_id:
            artifact_entries = await _list_workspace_artifact_entries_for_path(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                agent_run_id=artifact_agent_run_id,
            )
            if not artifact_entries:
                raise _build_workspace_artifact_not_found_http_exception(
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=False,
                    artifact_scope=artifact_scope,
                    agent_run_id=artifact_agent_run_id,
                )
            return _build_list_files_response(
                files=artifact_entries,
                identity_source=identity_source,
                sandbox_available=False,
                response_source="artifact",
                sandbox_file_count=0,
                artifact_file_count=len(artifact_entries),
                artifact_scope=artifact_scope,
                agent_run_id=artifact_agent_run_id,
            )

        if path.startswith("/workspace") and _is_workspace_artifact_only_binding_state(shadow_clone_binding_state):
            artifact_entries = await _list_workspace_artifact_entries_for_path(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
            )
            if not artifact_entries:
                raise _build_workspace_artifact_not_found_http_exception(
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=False,
                    binding_state=shadow_clone_binding_state,
                )
            _log_file_action_event(
                "info",
                "List files completed",
                request=request,
                status_code=200,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                requested_sandbox_id=sandbox_id,
                active_sandbox_id=active_sandbox_id,
                project_id=project_id,
                path=path,
                identity_source=identity_source,
                sandbox_available=False,
                response_source="artifact",
                outcome="fallback_success",
                file_count=len(artifact_entries),
                sandbox_file_count=0,
                artifact_file_count=len(artifact_entries),
                binding_state=shadow_clone_binding_state,
            )
            return _build_list_files_response(
                files=artifact_entries,
                identity_source=identity_source,
                sandbox_available=False,
                response_source="artifact",
                sandbox_file_count=0,
                artifact_file_count=len(artifact_entries),
            )

        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=requested_project_sandbox_id,
        )

        sandbox, active_sandbox_id, _ = await _get_project_sandbox_for_request(
            client,
            project_id=project_id,
            requested_sandbox_id=requested_project_sandbox_id,
            include_recent_shadow_clone_lease=True,
        )
        sandbox_available = True
        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=active_sandbox_id,
        )

        if path.startswith('/workspace'):
            try:
                if hasattr(sandbox, 'files') and hasattr(sandbox.files, 'make_dir'):
                    await _run_guarded_sandbox_io(
                        active_sandbox_id,
                        'files.make_dir',
                        lambda: sandbox.files.make_dir('/workspace'),
                        path='/workspace',
                    )
            except Exception as ensure_err:
                logger.debug(f"Workspace path ensure failed: {ensure_err}")

        files = await _run_guarded_sandbox_io(
            active_sandbox_id,
            'files.list',
            lambda: sandbox.files.list(path),
            path=path,
        )

        result: List[Dict[str, Any]] = []
        for file in files:
            full_path = f"{path.rstrip('/')}/{file.name}" if path != '/' else f"/{file.name}"
            if _is_hidden_workspace_path(full_path):
                continue

            result.append(_sandbox_entry_to_file_info(file, full_path))

        logger.info(f"Successfully listed {len(result)} files in sandbox {active_sandbox_id}")
        sandbox_payload = result
        artifact_entries: List[Dict[str, Any]] = []

        if path.startswith("/workspace"):
            try:
                artifact_entries = await _list_workspace_artifact_entries_for_path(
                    client,
                    project_id=project_id,
                    path=path,
                    include_hidden=include_hidden,
                )
            except Exception as artifact_error:
                logger.debug(f"Workspace artifact list failed (non-fatal): {artifact_error}")
                artifact_entries = []
            if artifact_entries:
                merged_payload = _merge_workspace_file_infos(sandbox_payload, artifact_entries)
                response_source = "mixed" if sandbox_payload else "artifact"
                _log_file_action_event(
                    "info",
                    "List files completed",
                    request=request,
                    status_code=200,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source=response_source,
                    outcome="success",
                    file_count=len(merged_payload),
                    sandbox_file_count=len(sandbox_payload),
                    artifact_file_count=len(artifact_entries),
                )
                return _build_list_files_response(
                    files=merged_payload,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source=response_source,
                    sandbox_file_count=len(sandbox_payload),
                    artifact_file_count=len(artifact_entries),
                )

        _log_file_action_event(
            "info",
            "List files completed",
            request=request,
            status_code=200,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            requested_sandbox_id=sandbox_id,
            active_sandbox_id=active_sandbox_id,
            project_id=project_id,
            path=path,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            response_source="sandbox",
            outcome="success",
            file_count=len(sandbox_payload),
            sandbox_file_count=len(sandbox_payload),
            artifact_file_count=0,
        )
        return _build_list_files_response(
            files=sandbox_payload,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            response_source="sandbox",
            sandbox_file_count=len(sandbox_payload),
            artifact_file_count=0,
        )
    except ShadowCloneStrictSandboxError as strict_error:
        sandbox_available = False
        if path.startswith("/workspace"):
            try:
                artifact_entries = await _list_workspace_artifact_entries_for_path(
                    client,
                    project_id=project_id,
                    path=path,
                    include_hidden=include_hidden,
                )
            except Exception as artifact_error:
                logger.debug(f"Workspace artifact fallback list failed: {artifact_error}")
                artifact_entries = []
            _log_file_action_event(
                "info",
                "List files completed",
                request=request,
                status_code=200,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                requested_sandbox_id=sandbox_id,
                active_sandbox_id=active_sandbox_id,
                project_id=project_id,
                path=path,
                identity_source=identity_source,
                sandbox_available=sandbox_available,
                response_source="artifact",
                outcome="fallback_success",
                file_count=len(artifact_entries),
                sandbox_file_count=0,
                artifact_file_count=len(artifact_entries),
                error_code=strict_error.error_code,
            )
            return _build_list_files_response(
                files=artifact_entries,
                identity_source=identity_source,
                sandbox_available=sandbox_available,
                response_source="artifact",
                sandbox_file_count=0,
                artifact_file_count=len(artifact_entries),
            )
        raise _shadow_clone_strict_error_to_http(strict_error)
    except HTTPException as http_error:
        sandbox_available = False
        if _can_use_workspace_artifact_fallback(
            path=path,
            project_id=project_id,
            http_error=http_error,
        ):
            try:
                artifact_entries = await _list_workspace_artifact_entries_for_path(
                    client,
                    project_id=project_id,
                    path=path,
                    include_hidden=include_hidden,
                )
            except Exception as artifact_error:
                logger.debug(f"Workspace artifact fallback list failed: {artifact_error}")
                artifact_entries = []
            if artifact_entries:
                _log_file_action_event(
                    "info",
                    "List files completed",
                    request=request,
                    status_code=200,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source="artifact",
                    outcome="fallback_success",
                    file_count=len(artifact_entries),
                    sandbox_file_count=0,
                    artifact_file_count=len(artifact_entries),
                    upstream_status_code=http_error.status_code,
                )
                return _build_list_files_response(
                    files=artifact_entries,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source="artifact",
                    sandbox_file_count=0,
                    artifact_file_count=len(artifact_entries),
                )
            raise _build_workspace_artifact_not_found_http_exception(
                path=path,
                identity_source=identity_source,
                sandbox_available=sandbox_available,
            )
        _log_file_action_event(
            "warning",
            "List files failed",
            request=request,
            status_code=http_error.status_code,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            requested_sandbox_id=sandbox_id,
            active_sandbox_id=active_sandbox_id,
            project_id=project_id,
            path=path,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            response_source=None,
            outcome="error",
            detail=str(http_error.detail),
        )
        raise http_error
    except Exception as e:
        sandbox_available = False
        logger.error(f"Error listing files in sandbox {sandbox_id}: {str(e)}")
        if _can_use_workspace_artifact_fallback(path=path, project_id=project_id):
            try:
                artifact_entries = await _list_workspace_artifact_entries_for_path(
                    client,
                    project_id=project_id,
                    path=path,
                    include_hidden=include_hidden,
                )
            except Exception as artifact_error:
                logger.debug(f"Workspace artifact fallback list failed: {artifact_error}")
                artifact_entries = []
            if artifact_entries:
                logger.info(
                    "Returning workspace artifact listing fallback for project=%s sandbox=%s path=%s (%s entries)",
                    project_id,
                    sandbox_id,
                    path,
                    len(artifact_entries),
                )
                _log_file_action_event(
                    "info",
                    "List files completed",
                    request=request,
                    status_code=200,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source="artifact",
                    outcome="fallback_success",
                    file_count=len(artifact_entries),
                    sandbox_file_count=0,
                    artifact_file_count=len(artifact_entries),
                    error=str(e),
                )
                return _build_list_files_response(
                    files=artifact_entries,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source="artifact",
                    sandbox_file_count=0,
                    artifact_file_count=len(artifact_entries),
                )
            raise _build_workspace_artifact_not_found_http_exception(
                path=path,
                identity_source=identity_source,
                sandbox_available=sandbox_available,
            )
        _log_file_action_event(
            "error",
            "List files failed",
            request=request,
            status_code=500,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            requested_sandbox_id=sandbox_id,
            active_sandbox_id=active_sandbox_id,
            project_id=project_id,
            path=path,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            response_source=None,
            outcome="error",
            error=str(e),
        )
        raise _sandbox_error_to_http(e, action='list files')

@router.get("/sandboxes/{sandbox_data:path}/files/content")
async def read_file_with_json_compatibility(
    sandbox_data: str,
    path: str,
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt)
):
    """
    处理前端传来的沙箱文件读取请求 - 兼容JSON对象路径格式
    支持两种格式:
    1. 正常格式: /api/sandboxes/{sandbox_id}/files/content?path=xxx
    2. JSON格式: /api/sandboxes/{"id":"xxx",...}/files/content?path=xxx
    """
    return await _handle_file_request(sandbox_data, path, request, user_id, "read")


async def _collect_workspace_files_recursive(
    sandbox: Any,
    sandbox_id: str,
    root_path: str,
    *,
    include_hidden: bool,
) -> List[str]:
    if sandbox is None:
        return []

    queue: List[str] = [root_path]
    visited: set[str] = set()
    collected_files: List[str] = []

    while queue:
        current_path = queue.pop()
        if current_path in visited:
            continue
        visited.add(current_path)

        if not include_hidden and _is_hidden_workspace_path(current_path):
            continue

        try:
            entries = await _run_guarded_sandbox_io(
                sandbox_id,
                'files.list',
                lambda current=current_path: sandbox.files.list(current),
                path=current_path,
            )
        except Exception as list_error:
            # A list failure on a file path typically means the path is a file.
            if current_path.startswith('/workspace/') and (
                _is_not_found_error(list_error) or _is_not_directory_error(list_error)
            ):
                collected_files.append(current_path)
                continue
            raise

        for entry in entries:
            full_path = f"{current_path.rstrip('/')}/{entry.name}" if current_path != '/' else f"/{entry.name}"
            if not include_hidden and _is_hidden_workspace_path(full_path):
                continue
            if _is_dir_entry(entry):
                queue.append(full_path)
            elif full_path.startswith('/workspace/'):
                collected_files.append(full_path)

    deduped: List[str] = []
    seen: set[str] = set()
    for file_path in collected_files:
        if file_path not in seen:
            deduped.append(file_path)
            seen.add(file_path)
    return deduped


@router.post('/sandboxes/{sandbox_id}/files/archive')
async def download_files_archive(
    sandbox_id: str,
    background_tasks: BackgroundTasks,
    payload: Optional[ArchiveDownloadRequest] = Body(default=None),
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    """Create a ZIP archive from workspace files and return it as a download."""
    started_at = time.monotonic()
    if not SANDBOX_ARCHIVE_DOWNLOAD_ENABLED:
        raise HTTPException(status_code=404, detail='Archive download endpoint is disabled')

    requested_sandbox_id = str(sandbox_id or "").strip()
    original_sandbox_id = requested_sandbox_id
    sandbox_id = await _resolve_sandbox_id(sandbox_id)
    artifact_agent_run_id = _parse_claude_local_agent_run_id(sandbox_id, original_sandbox_id, requested_sandbox_id)
    thread_workspace_thread_id = _parse_thread_workspace_thread_id(sandbox_id, original_sandbox_id, requested_sandbox_id)
    artifact_scope = _artifact_scope_for_agent_run(artifact_agent_run_id)
    payload = payload or ArchiveDownloadRequest()

    include_hidden = bool(payload.include_hidden) if payload.include_hidden is not None else False
    continue_on_error = bool(payload.continue_on_error) if payload.continue_on_error is not None else True

    client = await db.client
    if thread_workspace_thread_id:
        project_id, identity_source = await _resolve_thread_workspace_context(
            client,
            thread_id=thread_workspace_thread_id,
            user_id=user_id,
        )
        project_data = {"project_id": project_id, "sandbox": {}}
        project_sandbox_info = {}
        project_sandbox_id = requested_sandbox_id
        artifact_scope = "thread"
    else:
        project_data, identity_source = await _resolve_archive_project_context(
            client,
            requested_sandbox_id=requested_sandbox_id,
            resolved_sandbox_id=sandbox_id,
            original_sandbox_id=original_sandbox_id,
            user_id=user_id,
        )
        project_id = project_data.get('project_id')
        project_sandbox_info = _parse_sandbox_info(project_data.get('sandbox', {}))
        project_sandbox_id = str(
            project_sandbox_info.get('id')
            or sandbox_id
            or requested_sandbox_id
        ).strip()
    shadow_clone_binding_state: Optional[str] = None

    try:
        preferred_lease = await get_preferred_project_sandbox_lease(
            project_id,
            requested_sandbox_id=project_sandbox_id,
            include_recent=True,
        )
    except Exception as lease_error:
        logger.debug(
            "Archive preferred lease lookup failed",
            extra={
                "requested_sandbox_id": requested_sandbox_id,
                "resolved_sandbox_id": sandbox_id,
                "project_sandbox_id": project_sandbox_id,
                "project_id": project_id,
                "error": str(lease_error),
            },
        )
    else:
        if isinstance(preferred_lease, dict):
            shadow_clone_binding_state = (
                _normalize_shadow_clone_binding_state(preferred_lease.get("binding_state")) or None
            )

    logger.info(
        "Archive download requested",
        extra=_build_file_action_log_extra(
            request,
            **{
                "requested_sandbox_id": requested_sandbox_id,
                "resolved_sandbox_id": sandbox_id,
                "project_sandbox_id": project_sandbox_id,
                "project_id": project_id,
                "identity_source": identity_source,
                "has_explicit_paths": bool(payload.paths),
                "explicit_path_count": len(payload.paths or []),
                "root_path": payload.root_path or "/workspace",
                "include_hidden": include_hidden,
                "continue_on_error": continue_on_error,
                "artifact_store_state": workspace_artifacts.availability_state(),
                "binding_state": shadow_clone_binding_state,
                "artifact_scope": artifact_scope,
                "agent_run_id": artifact_agent_run_id,
            },
        ),
    )

    sandbox = None
    active_sandbox_id = project_sandbox_id or sandbox_id
    sandbox_available = False
    if artifact_agent_run_id or thread_workspace_thread_id:
        sandbox_available = False
    elif not _is_workspace_artifact_only_binding_state(shadow_clone_binding_state):
        try:
            await _touch_project_sandbox_access(
                client,
                project_id=project_id,
                sandbox_info=project_sandbox_info,
                fallback_sandbox_id=active_sandbox_id,
            )
            sandbox, active_sandbox_id, _ = await _get_project_sandbox_for_request(
                client,
                project_id=project_id,
                requested_sandbox_id=active_sandbox_id,
                include_recent_shadow_clone_lease=True,
            )
            sandbox_available = True
            await _touch_project_sandbox_access(
                client,
                project_id=project_id,
                sandbox_info=project_sandbox_info,
                fallback_sandbox_id=active_sandbox_id,
            )
        except ShadowCloneStrictSandboxError as strict_error:
            logger.warning(
                "Archive sandbox unavailable due to strict single-sandbox enforcement; using durable artifacts only: %s",
                strict_error,
            )
        except HTTPException as http_error:
            logger.warning(
                "Archive sandbox unavailable due to HTTP error; using durable artifacts only: %s",
                getattr(http_error, "detail", http_error),
            )
        except Exception as sandbox_error:
            logger.warning(
                "Archive sandbox unavailable; using durable artifacts only: %s",
                sandbox_error,
            )

    requested_paths: List[str] = []
    artifact_paths: List[str] = []
    report_root_path: Optional[str] = None
    artifact_store_state = workspace_artifacts.availability_state()

    if payload.paths:
        for raw_path in payload.paths:
            normalized = _normalize_workspace_path(raw_path)
            if not normalized.startswith('/workspace/'):
                raise HTTPException(status_code=400, detail=f'Only files under /workspace are supported: {normalized}')
            if not include_hidden and _is_hidden_workspace_path(normalized):
                continue
            sandbox_expanded_paths: Optional[List[str]] = None
            if sandbox is not None:
                try:
                    sandbox_expanded_paths = await _collect_workspace_files_recursive(
                        sandbox,
                        active_sandbox_id,
                        normalized,
                        include_hidden=include_hidden,
                    )
                except Exception as list_error:
                    logger.warning(
                        "Sandbox recursive listing failed for explicit archive path, continuing with durable artifacts",
                        extra={
                            "requested_sandbox_id": requested_sandbox_id,
                            "resolved_sandbox_id": sandbox_id,
                            "active_sandbox_id": active_sandbox_id,
                            "project_id": project_id,
                            "path": normalized,
                            "identity_source": identity_source,
                            "error": str(list_error),
                        },
                    )

            explicit_artifact_paths: List[str] = []
            try:
                if thread_workspace_thread_id:
                    explicit_artifact_paths = await workspace_artifacts.collect_thread_file_paths(
                        project_id=project_id,
                        thread_id=thread_workspace_thread_id,
                        root_path=normalized,
                        client=client,
                        include_hidden=include_hidden,
                    )
                else:
                    explicit_artifact_paths = await workspace_artifacts.collect_file_paths(
                        project_id=project_id,
                        root_path=normalized,
                        client=client,
                        include_hidden=include_hidden,
                        agent_run_id=artifact_agent_run_id,
                    )
            except Exception as artifact_list_error:
                logger.warning(
                    "Workspace artifact archive list failed for explicit path",
                    extra={
                        "requested_sandbox_id": requested_sandbox_id,
                        "resolved_sandbox_id": sandbox_id,
                        "active_sandbox_id": active_sandbox_id,
                        "project_id": project_id,
                        "path": normalized,
                        "identity_source": identity_source,
                        "error": str(artifact_list_error),
                    },
                )

            if sandbox_expanded_paths is not None:
                requested_paths.extend(sandbox_expanded_paths)
                for artifact_path in explicit_artifact_paths:
                    if artifact_path not in requested_paths:
                        requested_paths.append(artifact_path)
                continue

            if explicit_artifact_paths:
                requested_paths.extend(explicit_artifact_paths)
                continue

            requested_paths.append(normalized)
    else:
        root_path = _normalize_workspace_path(payload.root_path or '/workspace')
        report_root_path = root_path
        try:
            if thread_workspace_thread_id:
                artifact_paths = await workspace_artifacts.collect_thread_file_paths(
                    project_id=project_id,
                    thread_id=thread_workspace_thread_id,
                    root_path=root_path,
                    client=client,
                    include_hidden=include_hidden,
                )
            else:
                artifact_paths = await workspace_artifacts.collect_file_paths(
                    project_id=project_id,
                    root_path=root_path,
                    client=client,
                    include_hidden=include_hidden,
                    agent_run_id=artifact_agent_run_id,
                )
        except Exception as artifact_list_error:
            logger.warning(
                "Workspace artifact archive list failed",
                extra={
                    "requested_sandbox_id": requested_sandbox_id,
                    "resolved_sandbox_id": sandbox_id,
                    "active_sandbox_id": active_sandbox_id,
                    "project_id": project_id,
                    "root_path": root_path,
                    "identity_source": identity_source,
                    "error": str(artifact_list_error),
                },
            )
            artifact_paths = []
        artifact_store_state = workspace_artifacts.availability_state()

        if sandbox is not None:
            try:
                requested_paths = await _collect_workspace_files_recursive(
                    sandbox,
                    active_sandbox_id,
                    root_path,
                    include_hidden=include_hidden,
                )
            except Exception as list_error:
                logger.warning(
                    "Sandbox recursive listing failed for archive, continuing with durable artifacts",
                    extra={
                        "requested_sandbox_id": requested_sandbox_id,
                        "resolved_sandbox_id": sandbox_id,
                        "active_sandbox_id": active_sandbox_id,
                        "project_id": project_id,
                        "root_path": root_path,
                        "identity_source": identity_source,
                        "error": str(list_error),
                    },
                )
                requested_paths = []

        if artifact_paths:
            for artifact_path in artifact_paths:
                if artifact_path not in requested_paths:
                    requested_paths.append(artifact_path)

    deduped_paths: List[str] = []
    seen_paths: set[str] = set()
    for candidate in requested_paths:
        if candidate not in seen_paths:
            deduped_paths.append(candidate)
            seen_paths.add(candidate)

    if not deduped_paths:
        detail = {
            "message": "No files found to archive",
            "error_code": "ARCHIVE_NO_FILES_FOUND",
            "requested_sandbox_id": requested_sandbox_id,
            "resolved_sandbox_id": sandbox_id,
            "active_sandbox_id": active_sandbox_id,
            "project_id": project_id,
            "identity_source": identity_source,
            "sandbox_available": sandbox_available,
            "artifact_store_state": artifact_store_state,
            "artifact_path_count": len(artifact_paths),
            "requested_path_count": len(requested_paths),
            "root_path": report_root_path,
            "artifact_scope": artifact_scope,
            "agent_run_id": artifact_agent_run_id,
            "status_code": 404,
        }
        logger.warning("Archive request found no files", extra=_build_file_action_log_extra(request, **detail))
        raise HTTPException(
            status_code=404,
            detail=detail,
            headers=_build_archive_observability_headers(
                request=request,
                response_source="artifact" if not sandbox_available else "sandbox",
                identity_source=identity_source,
                sandbox_available=sandbox_available,
                outcome="error",
            ),
        )

    fd, temp_zip_path = tempfile.mkstemp(prefix=f'sandbox-archive-{active_sandbox_id}-', suffix='.zip')
    os.close(fd)

    success_files: List[str] = []
    failed_files: List[Dict[str, Any]] = []
    report: Dict[str, Any] = {}
    live_success_count = 0
    artifact_success_count = 0

    try:
        with zipfile.ZipFile(temp_zip_path, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
            for file_path in deduped_paths:
                archive_relative_path = _relative_workspace_path(file_path)
                if not archive_relative_path:
                    archive_relative_path = os.path.basename(file_path) or 'file'

                file_content = None
                live_read_error: Optional[Exception] = None
                try:
                    if sandbox is None:
                        raise RuntimeError("sandbox unavailable for archive read")
                    file_content = await _run_guarded_sandbox_io(
                        active_sandbox_id,
                        'files.read',
                        lambda current=file_path: sandbox.files.read(current, format='bytes'),
                        path=file_path,
                    )
                except Exception as file_error:
                    live_read_error = file_error

                if file_content is not None:
                    archive.writestr(archive_relative_path, _to_response_bytes(file_content))
                    success_files.append(file_path)
                    live_success_count += 1
                    continue

                try:
                    durable_payload = None
                    if thread_workspace_thread_id:
                        durable_payload = await workspace_artifacts.read_thread_artifact_bytes(
                            project_id=project_id,
                            thread_id=thread_workspace_thread_id,
                            path=file_path,
                            client=client,
                            include_hidden=include_hidden,
                        )
                    else:
                        durable_payload = await workspace_artifacts.read_artifact_bytes(
                            project_id=project_id,
                            path=file_path,
                            client=client,
                            include_hidden=include_hidden,
                            agent_run_id=artifact_agent_run_id,
                        )
                except Exception as durable_error:
                    durable_payload = None
                    logger.warning(
                        "Workspace artifact archive read failed",
                        extra={
                            "requested_sandbox_id": requested_sandbox_id,
                            "resolved_sandbox_id": sandbox_id,
                            "active_sandbox_id": active_sandbox_id,
                            "project_id": project_id,
                            "file_path": file_path,
                            "identity_source": identity_source,
                            "error": str(durable_error),
                        },
                    )

                artifact_store_state = workspace_artifacts.availability_state()
                if durable_payload is not None:
                    _, durable_bytes = durable_payload
                    archive.writestr(archive_relative_path, durable_bytes)
                    success_files.append(file_path)
                    artifact_success_count += 1
                    continue

                error_payload = _archive_error_payload(
                    live_read_error or RuntimeError("archive read failed without a sandbox error")
                )
                error_payload["artifact_store_state"] = artifact_store_state
                failed_files.append({'path': file_path, **error_payload})
                logger.warning(
                    "Archive read failed for file",
                    extra={
                        "requested_sandbox_id": requested_sandbox_id,
                        "resolved_sandbox_id": sandbox_id,
                        "active_sandbox_id": active_sandbox_id,
                        "project_id": project_id,
                        "file_path": file_path,
                        "identity_source": identity_source,
                        "sandbox_available": sandbox_available,
                        "error_payload": error_payload,
                    },
                )
                if not continue_on_error:
                    break

            report = {
                'generated_at': datetime.utcnow().isoformat() + 'Z',
                'sandbox_id': active_sandbox_id,
                'requested_sandbox_id': requested_sandbox_id,
                'resolved_sandbox_id': sandbox_id,
                'project_id': project_id,
                'identity_source': identity_source,
                'sandbox_available': sandbox_available,
                'artifact_store_state': artifact_store_state,
                'response_source': _classify_archive_response_source(
                    live_success_count=live_success_count,
                    artifact_success_count=artifact_success_count,
                ),
                'root_path': report_root_path,
                'requested_paths': deduped_paths,
                'success_files': success_files,
                'failed_files': failed_files,
                'summary': {
                    'total': len(deduped_paths),
                    'succeeded': len(success_files),
                    'failed': len(failed_files),
                },
            }
            archive.writestr('_download_report.json', json.dumps(report, ensure_ascii=False, indent=2))

        if not success_files:
            _cleanup_temp_file(temp_zip_path)
            logger.warning(
                "Archive generation failed because all file downloads failed",
                extra=_build_file_action_log_extra(
                    request,
                    **{
                        "requested_sandbox_id": requested_sandbox_id,
                        "resolved_sandbox_id": sandbox_id,
                        "active_sandbox_id": active_sandbox_id,
                        "project_id": project_id,
                        "identity_source": identity_source,
                        "sandbox_available": sandbox_available,
                        "artifact_store_state": artifact_store_state,
                        "requested_path_count": len(deduped_paths),
                        "artifact_path_count": len(artifact_paths),
                        "failed_count": len(failed_files),
                        "status_code": 424,
                        "duration_ms": int((time.monotonic() - started_at) * 1000),
                        "response_source": _classify_archive_response_source(
                            live_success_count=live_success_count,
                            artifact_success_count=artifact_success_count,
                        ),
                        "outcome": "error",
                    },
                ),
            )
            raise HTTPException(
                status_code=424,
                detail={
                    'message': 'Archive generation failed because all file downloads failed',
                    'error_code': 'ARCHIVE_ALL_READS_FAILED',
                    'artifact_store_state': artifact_store_state,
                    'report': report,
                },
                headers=_build_archive_observability_headers(
                    request=request,
                    response_source=_classify_archive_response_source(
                        live_success_count=live_success_count,
                        artifact_success_count=artifact_success_count,
                    ),
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    outcome="error",
                ),
            )

        archive_filename = _build_archive_filename(active_sandbox_id)
        archive_response_source = _classify_archive_response_source(
            live_success_count=live_success_count,
            artifact_success_count=artifact_success_count,
        )
        archive_outcome = "partial_success" if failed_files else "success"
        headers = _build_archive_download_headers(
            request=request,
            filename=archive_filename,
            total_count=len(deduped_paths),
            succeeded_count=len(success_files),
            failed_count=len(failed_files),
            response_source=archive_response_source,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            outcome=archive_outcome,
        )

        background_tasks.add_task(_cleanup_temp_file, temp_zip_path)

        logger.info(
            "Archive generation completed",
            extra=_build_file_action_log_extra(
                request,
                **{
                    "requested_sandbox_id": requested_sandbox_id,
                    "resolved_sandbox_id": sandbox_id,
                    "active_sandbox_id": active_sandbox_id,
                    "project_id": project_id,
                    "identity_source": identity_source,
                    "sandbox_available": sandbox_available,
                    "artifact_store_state": artifact_store_state,
                    "requested_path_count": len(deduped_paths),
                    "artifact_path_count": len(artifact_paths),
                    "succeeded": len(success_files),
                    "failed": len(failed_files),
                    "response_source": archive_response_source,
                    "outcome": archive_outcome,
                    "status_code": 200,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                },
            ),
        )
        return FileResponse(
            path=temp_zip_path,
            media_type='application/zip',
            filename=archive_filename,
            background=background_tasks,
            headers=headers,
        )
    except HTTPException as http_error:
        if not getattr(http_error, "headers", None):
            http_error.headers = {}
        http_error.headers = {
            **_build_archive_observability_headers(
                request=request,
                response_source=(
                    _classify_archive_response_source(
                        live_success_count=live_success_count,
                        artifact_success_count=artifact_success_count,
                    )
                    if (live_success_count or artifact_success_count)
                    else ("artifact" if not sandbox_available else "sandbox")
                ),
                identity_source=identity_source,
                sandbox_available=sandbox_available,
                outcome="error",
            ),
            **dict(http_error.headers or {}),
        }
        raise
    except Exception as archive_error:
        _cleanup_temp_file(temp_zip_path)
        logger.error(
            "Archive generation failed",
            extra=_build_file_action_log_extra(
                request,
                **{
                    "requested_sandbox_id": requested_sandbox_id,
                    "resolved_sandbox_id": sandbox_id,
                    "active_sandbox_id": active_sandbox_id,
                    "project_id": project_id,
                    "identity_source": identity_source,
                    "sandbox_available": sandbox_available,
                    "artifact_store_state": artifact_store_state,
                    "requested_path_count": len(deduped_paths),
                    "artifact_path_count": len(artifact_paths),
                    "error": str(archive_error),
                    "status_code": 500,
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                    "outcome": "error",
                },
            ),
        )
        raise _sandbox_error_to_http(archive_error, action='archive files')


@router.get("/sandboxes/{malformed_url:path}")
async def handle_malformed_file_url(
    malformed_url: str,
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt)
):
    """
    处理前端发送的错误URL格式，例如:
    /sandboxes/{...}/files/content&path=xxx (使用&而不是?)
    """
    logger.info(f"🔧 收到错误格式URL: {malformed_url}")
    
    # 检查是否是files/content&path=格式
    if "/files/content&path=" in malformed_url:
        # 分离sandbox_data和path
        parts = malformed_url.split("/files/content&path=", 1)
        if len(parts) == 2:
            sandbox_data = parts[0]
            path = urllib.parse.unquote(parts[1])
            logger.info(f"🔧 解析出sandbox_data: {sandbox_data}, path: {path}")
            return await _handle_file_request(sandbox_data, path, request, user_id, "read")
    
    # 检查是否是files&path=格式
    if "/files&path=" in malformed_url:
        parts = malformed_url.split("/files&path=", 1) 
        if len(parts) == 2:
            sandbox_data = parts[0]
            path = urllib.parse.unquote(parts[1])
            logger.info(f"🔧 解析出sandbox_data: {sandbox_data}, path: {path}")
            return await _handle_file_request(sandbox_data, path, request, user_id, "list")
    
    raise HTTPException(status_code=404, detail="Invalid URL format")


async def _handle_file_request(
    sandbox_data: str,
    path: str,
    request: Request,
    user_id: str,
    action: str  # "read" or "list"
):
    """
    统一处理文件请求（读取或列表），支持JSON格式的sandbox参数
    """
    # 尝试解析sandbox_data
    sandbox_id = None
    try:
        # 检查是否是JSON格式
        if sandbox_data.startswith('{') and sandbox_data.endswith('}'):
            logger.info(f"🔧 检测到JSON格式的sandbox参数，正在解析...")
            sandbox_obj = json.loads(sandbox_data)
            sandbox_id = sandbox_obj.get('id')
            logger.info(f"✅ 成功从JSON中提取sandbox_id: {sandbox_id}")
        else:
            # 普通字符串格式
            sandbox_id = sandbox_data
            logger.info(f"📝 使用普通格式sandbox_id: {sandbox_id}")
    except json.JSONDecodeError as e:
        logger.error(f"❌ JSON解析失败: {e}")
        # 降级到普通字符串处理
        sandbox_id = sandbox_data
    except Exception as e:
        logger.error(f"❌ 处理sandbox参数时出错: {e}")
        raise HTTPException(status_code=400, detail="Invalid sandbox parameter format")
    
    if not sandbox_id:
        raise HTTPException(status_code=400, detail="Sandbox ID not found in parameter")

    original_sandbox_id = sandbox_id
    sandbox_id = await _resolve_sandbox_id(sandbox_id)

    # 根据action类型调用相应的处理函数
    if action == "read":
        return await _read_file_internal(sandbox_id, path, request, user_id, original_sandbox_id=original_sandbox_id)
    elif action == "list":
        return await _list_files_internal(sandbox_id, path, request, user_id, original_sandbox_id=original_sandbox_id)
    else:
        raise HTTPException(status_code=400, detail="Invalid action type")


async def _read_file_internal(
    sandbox_id: str,
    path: str,
    request: Request = None,
    user_id: str = None,
    original_sandbox_id: str = None,
):
    """Read a file from the sandbox"""
    started_at = time.monotonic()
    original_path = path
    path = normalize_path(path)

    logger.info(f"Received file read request for sandbox {sandbox_id}, path: {path}, user_id: {user_id}")
    if original_path != path:
        logger.info(f"Normalized path from '{original_path}' to '{path}'")

    client = await db.client
    include_hidden = False
    project_id: Optional[str] = None
    active_sandbox_id = sandbox_id
    sandbox_available = False
    identity_source = "unresolved"
    project_sandbox_info: Dict[str, Any] = {}
    requested_project_sandbox_id = sandbox_id
    artifact_agent_run_id = _parse_claude_local_agent_run_id(sandbox_id, original_sandbox_id)
    artifact_scope = _artifact_scope_for_agent_run(artifact_agent_run_id)
    thread_workspace_thread_id = _parse_thread_workspace_thread_id(sandbox_id, original_sandbox_id)
    try:
        if path.startswith("/workspace") and thread_workspace_thread_id:
            project_id, identity_source = await _resolve_thread_workspace_context(
                client,
                thread_id=thread_workspace_thread_id,
                user_id=user_id,
            )
            artifact_response = await _read_thread_workspace_artifact_response(
                client,
                project_id=project_id,
                thread_id=thread_workspace_thread_id,
                path=path,
                include_hidden=include_hidden,
                request=request,
            )
            if artifact_response is not None:
                return artifact_response
            directory_entries = await _list_thread_workspace_artifact_entries_for_path(
                client,
                project_id=project_id,
                thread_id=thread_workspace_thread_id,
                path=path,
                include_hidden=include_hidden,
            )
            if directory_entries:
                raise _build_workspace_directory_http_exception(
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=False,
                    directory_source="thread_workspace",
                    artifact_scope="thread",
                )
            raise _thread_workspace_not_found_http_exception(
                path=path,
                identity_source=identity_source,
                thread_id=thread_workspace_thread_id,
            )

        delivery_context = await resolve_file_delivery_context(
            client=client,
            requested_sandbox_id=sandbox_id,
            resolved_sandbox_id=sandbox_id,
            original_sandbox_id=original_sandbox_id,
            user_id=user_id,
            verify_sandbox_access=verify_sandbox_access,
            preferred_lease_resolver=get_preferred_project_sandbox_lease,
        )
        project_id = delivery_context.project_id
        project_sandbox_info = delivery_context.project_sandbox_info
        requested_project_sandbox_id = delivery_context.project_sandbox_id or sandbox_id
        active_sandbox_id = requested_project_sandbox_id
        identity_source = delivery_context.identity_source
        shadow_clone_binding_state = delivery_context.shadow_clone_binding_state

        if path.startswith("/workspace") and artifact_agent_run_id:
            directory_http_error = await _maybe_build_workspace_directory_http_exception(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                identity_source=identity_source,
                sandbox_available=False,
                agent_run_id=artifact_agent_run_id,
            )
            if directory_http_error is not None:
                raise directory_http_error
            artifact_response = await _read_workspace_artifact_response(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                request=request,
                agent_run_id=artifact_agent_run_id,
            )
            if artifact_response is not None:
                return artifact_response
            raise _build_workspace_artifact_not_found_http_exception(
                path=path,
                identity_source=identity_source,
                sandbox_available=False,
                artifact_scope=artifact_scope,
                agent_run_id=artifact_agent_run_id,
            )

        if path.startswith("/workspace") and _is_workspace_artifact_only_binding_state(shadow_clone_binding_state):
            directory_http_error = await _maybe_build_workspace_directory_http_exception(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                identity_source=identity_source,
                sandbox_available=False,
            )
            if directory_http_error is not None:
                raise directory_http_error
            artifact_response = await _read_workspace_artifact_response(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                request=request,
            )
            if artifact_response is not None:
                _log_file_action_event(
                    "info",
                    "File download completed",
                    request=request,
                    status_code=200,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=False,
                    response_source="artifact",
                    outcome="fallback_success",
                    content_length=_response_content_length(artifact_response),
                    binding_state=shadow_clone_binding_state,
                )
                return artifact_response
            raise _build_workspace_artifact_not_found_http_exception(
                path=path,
                identity_source=identity_source,
                sandbox_available=False,
                binding_state=shadow_clone_binding_state,
            )

        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=requested_project_sandbox_id,
        )
        sandbox, active_sandbox_id, _ = await _get_project_sandbox_for_request(
            client,
            project_id=project_id,
            requested_sandbox_id=requested_project_sandbox_id,
            include_recent_shadow_clone_lease=True,
        )
        sandbox_available = True
        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=active_sandbox_id,
        )
        try:
            content = await _run_guarded_sandbox_io(
                active_sandbox_id,
                'files.read',
                lambda: sandbox.files.read(path, format='bytes'),
                path=path,
            )
        except Exception as download_err:
            sandbox_available = False
            logger.error(f"Error downloading file {path} from sandbox {active_sandbox_id}: {str(download_err)}")
            if _is_directory_error(download_err):
                raise _build_workspace_directory_http_exception(
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    directory_source="sandbox",
                )
            if isinstance(download_err, (SandboxIOTimeoutError, SandboxIOQueueTimeoutError)):
                if path.startswith("/workspace"):
                    artifact_response = await _read_workspace_artifact_response(
                        client,
                        project_id=project_id,
                        path=path,
                        include_hidden=include_hidden,
                        request=request,
                    )
                    if artifact_response is not None:
                        _log_file_action_event(
                            "info",
                            "File download completed",
                            request=request,
                            status_code=200,
                            duration_ms=int((time.monotonic() - started_at) * 1000),
                            requested_sandbox_id=sandbox_id,
                            active_sandbox_id=active_sandbox_id,
                            project_id=project_id,
                            path=path,
                            identity_source=identity_source,
                            sandbox_available=sandbox_available,
                            response_source="artifact",
                            outcome="fallback_success",
                            content_length=_response_content_length(artifact_response),
                        )
                        return artifact_response
                raise _sandbox_error_to_http(download_err, action='download file')
            if _is_not_found_error(download_err):
                if path.startswith("/workspace"):
                    artifact_response = await _read_workspace_artifact_response(
                        client,
                        project_id=project_id,
                        path=path,
                        include_hidden=include_hidden,
                        request=request,
                    )
                    if artifact_response is not None:
                        _log_file_action_event(
                            "info",
                            "File download completed",
                            request=request,
                            status_code=200,
                            duration_ms=int((time.monotonic() - started_at) * 1000),
                            requested_sandbox_id=sandbox_id,
                            active_sandbox_id=active_sandbox_id,
                            project_id=project_id,
                            path=path,
                            identity_source=identity_source,
                            sandbox_available=sandbox_available,
                            response_source="artifact",
                            outcome="fallback_success",
                            content_length=_response_content_length(artifact_response),
                        )
                        return artifact_response
                raise HTTPException(status_code=404, detail=f"Failed to download file: {str(download_err)}")
            if path.startswith("/workspace"):
                directory_http_error = await _maybe_build_workspace_directory_http_exception(
                    client,
                    project_id=project_id,
                    path=path,
                    include_hidden=include_hidden,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                )
                if directory_http_error is not None:
                    raise directory_http_error
                artifact_response = await _read_workspace_artifact_response(
                    client,
                    project_id=project_id,
                    path=path,
                    include_hidden=include_hidden,
                    request=request,
                )
                if artifact_response is not None:
                    _log_file_action_event(
                        "info",
                        "File download completed",
                        request=request,
                        status_code=200,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                        requested_sandbox_id=sandbox_id,
                        active_sandbox_id=active_sandbox_id,
                        project_id=project_id,
                        path=path,
                        identity_source=identity_source,
                        sandbox_available=sandbox_available,
                        response_source="artifact",
                        outcome="fallback_success",
                        content_length=_response_content_length(artifact_response),
                    )
                    return artifact_response
            raise HTTPException(status_code=500, detail=f"Failed to download file: {str(download_err)}")

        data = _to_response_bytes(content)
        filename = os.path.basename(path)
        logger.info(f"Successfully read file {filename} from sandbox {active_sandbox_id}")

        response = Response(
            content=data,
            media_type=guess_workspace_artifact_content_type(path, content),
            headers=_build_file_download_headers(
                filename=filename,
                request=request,
                response_source="sandbox",
            ),
        )
        _log_file_action_event(
            "info",
            "File download completed",
            request=request,
            status_code=200,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            requested_sandbox_id=sandbox_id,
            active_sandbox_id=active_sandbox_id,
            project_id=project_id,
            path=path,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            response_source="sandbox",
            outcome="success",
            content_length=len(data),
        )
        return response
    except ShadowCloneStrictSandboxError as strict_error:
        sandbox_available = False
        if path.startswith("/workspace"):
            directory_http_error = await _maybe_build_workspace_directory_http_exception(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                identity_source=identity_source,
                sandbox_available=sandbox_available,
            )
            if directory_http_error is not None:
                _log_file_action_event(
                    "warning",
                    "File download failed",
                    request=request,
                    status_code=directory_http_error.status_code,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source=None,
                    outcome="error",
                    detail=str(directory_http_error.detail),
                    error_code=strict_error.error_code,
                )
                raise directory_http_error
            artifact_response = await _read_workspace_artifact_response(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                request=request,
            )
            if artifact_response is not None:
                _log_file_action_event(
                    "info",
                    "File download completed",
                    request=request,
                    status_code=200,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source="artifact",
                    outcome="fallback_success",
                    content_length=_response_content_length(artifact_response),
                    error_code=strict_error.error_code,
                )
                return artifact_response
            raise HTTPException(
                status_code=404,
                detail="File not found (sandbox unavailable and durable artifact missing)",
            )
        raise _shadow_clone_strict_error_to_http(strict_error)
    except HTTPException as http_error:
        sandbox_available = False
        if _can_use_workspace_artifact_fallback(
            path=path,
            project_id=project_id,
            http_error=http_error,
        ):
            directory_http_error = await _maybe_build_workspace_directory_http_exception(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                identity_source=identity_source,
                sandbox_available=sandbox_available,
            )
            if directory_http_error is not None:
                http_error = directory_http_error
            else:
                artifact_response = await _read_workspace_artifact_response(
                    client,
                    project_id=project_id,
                    path=path,
                    include_hidden=include_hidden,
                    request=request,
                )
                if artifact_response is not None:
                    _log_file_action_event(
                        "info",
                        "File download completed",
                        request=request,
                        status_code=200,
                        duration_ms=int((time.monotonic() - started_at) * 1000),
                        requested_sandbox_id=sandbox_id,
                        active_sandbox_id=active_sandbox_id,
                        project_id=project_id,
                        path=path,
                        identity_source=identity_source,
                        sandbox_available=sandbox_available,
                        response_source="artifact",
                        outcome="fallback_success",
                        content_length=_response_content_length(artifact_response),
                        upstream_status_code=http_error.status_code,
                    )
                    return artifact_response
                if http_error.status_code not in {429, 504}:
                    http_error = HTTPException(
                        status_code=404,
                        detail="File not found (sandbox unavailable and durable artifact missing)",
                    )
            if http_error.status_code in {429, 504}:
                _log_file_action_event(
                    "warning",
                    "File download failed",
                    request=request,
                    status_code=http_error.status_code,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source=None,
                    outcome="error",
                    detail=str(http_error.detail),
                )
                raise http_error
        _log_file_action_event(
            "warning",
            "File download failed",
            request=request,
            status_code=http_error.status_code,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            requested_sandbox_id=sandbox_id,
            active_sandbox_id=active_sandbox_id,
            project_id=project_id,
            path=path,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            response_source=None,
            outcome="error",
            detail=str(http_error.detail),
        )
        raise http_error
    except Exception as e:
        sandbox_available = False
        logger.error(f"Error reading file in sandbox {sandbox_id}: {str(e)}")
        if _can_use_workspace_artifact_fallback(path=path, project_id=project_id):
            directory_http_error = await _maybe_build_workspace_directory_http_exception(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                identity_source=identity_source,
                sandbox_available=sandbox_available,
            )
            if directory_http_error is not None:
                _log_file_action_event(
                    "warning",
                    "File download failed",
                    request=request,
                    status_code=directory_http_error.status_code,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source=None,
                    outcome="error",
                    detail=str(directory_http_error.detail),
                    error=str(e),
                )
                raise directory_http_error
            artifact_response = await _read_workspace_artifact_response(
                client,
                project_id=project_id,
                path=path,
                include_hidden=include_hidden,
                request=request,
            )
            if artifact_response is not None:
                _log_file_action_event(
                    "info",
                    "File download completed",
                    request=request,
                    status_code=200,
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    requested_sandbox_id=sandbox_id,
                    active_sandbox_id=active_sandbox_id,
                    project_id=project_id,
                    path=path,
                    identity_source=identity_source,
                    sandbox_available=sandbox_available,
                    response_source="artifact",
                    outcome="fallback_success",
                    content_length=_response_content_length(artifact_response),
                    error=str(e),
                )
                return artifact_response
        _log_file_action_event(
            "error",
            "File download failed",
            request=request,
            status_code=500,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            requested_sandbox_id=sandbox_id,
            active_sandbox_id=active_sandbox_id,
            project_id=project_id,
            path=path,
            identity_source=identity_source,
            sandbox_available=sandbox_available,
            response_source=None,
            outcome="error",
            error=str(e),
        )
        raise _sandbox_error_to_http(e, action='read file')


@router.delete("/sandboxes/{sandbox_id}/files")
async def delete_file(
    sandbox_id: str, 
    path: str,
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Delete a file from the sandbox"""
    original_sandbox_id = sandbox_id
    sandbox_id = await _resolve_sandbox_id(sandbox_id)
    # Normalize the path to handle UTF-8 encoding correctly
    path = normalize_path(path)

    logger.info(f"Received file delete request for sandbox {sandbox_id}, path: {path}, user_id: {user_id}")
    client = await db.client

    # Verify the user has access to this sandbox
    project_data = await verify_sandbox_access(
        client,
        sandbox_id,
        user_id,
        original_sandbox_id=original_sandbox_id,
    )
    project_id = project_data.get('project_id')
    project_sandbox_info = _parse_sandbox_info(project_data.get('sandbox', {}))
    
    try:
        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=sandbox_id,
        )
        sandbox, active_sandbox_id, _ = await _get_project_sandbox_for_request(
            client,
            project_id=project_id,
            requested_sandbox_id=sandbox_id,
            include_recent_shadow_clone_lease=True,
        )
        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=project_sandbox_info,
            fallback_sandbox_id=active_sandbox_id,
        )

        await _run_guarded_sandbox_io(
            active_sandbox_id,
            'commands.run',
            lambda: sandbox.commands.run(f"rm -f {shlex.quote(path)}"),
            path=path,
        )
        logger.info(f"File deleted at {path} in sandbox {active_sandbox_id}")

        if path.startswith("/workspace"):
            try:
                await workspace_artifacts.delete_artifact(
                    project_id=project_id,
                    path=path,
                    client=client,
                    include_hidden=False,
                )
            except Exception as delete_error:
                logger.warning(
                    "Workspace artifact delete failed for project=%s path=%s: %s",
                    project_id,
                    path,
                    delete_error,
                )

        return {"status": "success", "deleted": True, "path": path}
    except ShadowCloneStrictSandboxError as strict_error:
        if path.startswith("/workspace"):
            try:
                await workspace_artifacts.delete_artifact(
                    project_id=project_id,
                    path=path,
                    client=client,
                    include_hidden=False,
                )
            except Exception as delete_error:
                logger.debug(f"Workspace artifact fallback delete failed: {delete_error}")
            return {"status": "success", "deleted": True, "path": path, "sandbox_deleted": False}
        raise _shadow_clone_strict_error_to_http(strict_error)
    except HTTPException as http_error:
        if path.startswith("/workspace"):
            try:
                await workspace_artifacts.delete_artifact(
                    project_id=project_id,
                    path=path,
                    client=client,
                    include_hidden=False,
                )
            except Exception as delete_error:
                logger.debug(f"Workspace artifact fallback delete failed: {delete_error}")
            return {"status": "success", "deleted": True, "path": path, "sandbox_deleted": False}
        raise http_error
    except Exception as e:
        logger.error(f"Error deleting file in sandbox {sandbox_id}: {str(e)}")
        if path.startswith("/workspace"):
            try:
                await workspace_artifacts.delete_artifact(
                    project_id=project_id,
                    path=path,
                    client=client,
                    include_hidden=False,
                )
                return {"status": "success", "deleted": True, "path": path, "sandbox_deleted": False}
            except Exception as delete_error:
                logger.debug(f"Workspace artifact fallback delete failed: {delete_error}")
        raise _sandbox_error_to_http(e, action='delete file')

@router.delete("/sandboxes/{sandbox_id}")
async def delete_sandbox_route(
    sandbox_id: str,
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt)
):
    """Delete an entire sandbox"""
    original_sandbox_id = sandbox_id
    sandbox_id = await _resolve_sandbox_id(sandbox_id)
    logger.info(f"Received sandbox delete request for sandbox {sandbox_id}, user_id: {user_id}")
    client = await db.client

    # Verify the user has access to this sandbox
    await verify_sandbox_access(client, sandbox_id, user_id, original_sandbox_id=original_sandbox_id)
    
    try:
        # Delete the sandbox using the sandbox module function
        await delete_sandbox(sandbox_id)
        
        return {"status": "success", "deleted": True, "sandbox_id": sandbox_id}
    except Exception as e:
        logger.error(f"Error deleting sandbox {sandbox_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

# Should happen on server-side fully
@router.post("/project/{project_id}/sandbox/ensure-active")
async def ensure_project_sandbox_active(
    project_id: str,
    request: Request = None,
    user_id: str = Depends(get_current_user_id_from_jwt)
):
    """
    Ensure that a project's sandbox is active and running.
    Checks the sandbox status and starts it if it's not running.
    """
    logger.info(f"Received ensure sandbox active request for project {project_id}, user_id: {user_id}")
    client = await db.client
    
    # Find the project and sandbox information
    project_result = await client.table('projects').select('*').eq('project_id', project_id).execute()
    
    if not project_result.data or len(project_result.data) == 0:
        logger.error(f"Project not found: {project_id}")
        raise HTTPException(status_code=404, detail="Project not found")
    
    project_data = project_result.data[0]
    
    # For public projects, no authentication is needed
    if not project_data.get('is_public'):
        # For private projects, we must have a user_id
        if not user_id:
            logger.error(f"Authentication required for private project {project_id}")
            raise HTTPException(status_code=401, detail="Authentication required for this resource")
            
        # Direct ownership check: in this project, account_id directly stores user_id
        account_id = project_data.get('account_id')
        if account_id and account_id != user_id:
            logger.error(f"User {user_id} not authorized to access project {project_id}")
            raise HTTPException(status_code=403, detail="Not authorized to access this project")
    
    try:
        # Get sandbox ID from project data
        sandbox_info = project_data.get('sandbox', {})
        if isinstance(sandbox_info, str):
            sandbox_info = json.loads(sandbox_info)

        if not sandbox_info.get('id'):
            raise HTTPException(status_code=404, detail="No sandbox found for this project")
            
        sandbox_id = sandbox_info['id']
        
        # Get sandbox without allowing Shadow Clone runs to fail over to a new one.
        logger.info(f"Ensuring sandbox is active for project {project_id}")
        preferred_lease = await get_preferred_project_sandbox_lease(
            project_id,
            requested_sandbox_id=sandbox_id,
            include_recent=True,
        )
        if preferred_lease is not None:
            sandbox = await attach_shadow_clone_bound_sandbox(
                client,
                lease=preferred_lease,
            )
        else:
            sandbox = await get_sandbox_by_id_safely(client, sandbox_id, project_id=project_id)
        resolved_id = (
            getattr(sandbox, "sandbox_id", None)
            or getattr(sandbox, "id", None)
            or sandbox_id
        )

        await _touch_project_sandbox_access(
            client,
            project_id=project_id,
            sandbox_info=sandbox_info,
            fallback_sandbox_id=str(resolved_id),
        )

        refreshed_project = await (
            client.table('projects')
            .select('sandbox')
            .eq('project_id', project_id)
            .execute()
        )
        refreshed_sandbox_info = sandbox_info
        if refreshed_project.data:
            refreshed_sandbox_info = _parse_sandbox_info(
                refreshed_project.data[0].get('sandbox', {}),
            )
        
        logger.info(f"Successfully ensured sandbox {resolved_id} is active for project {project_id}")
        
        return {
            "status": "success",
            "sandbox_id": resolved_id,
            "resolved_sandbox_id": resolved_id,
            "sandbox": _build_sandbox_response_payload(refreshed_sandbox_info),
            "message": "Sandbox is active"
        }
    except ShadowCloneStrictSandboxError as strict_error:
        raise _shadow_clone_strict_error_to_http(strict_error)
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error ensuring sandbox is active for project {project_id}: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Sandbox persistence endpoints
# ---------------------------------------------------------------------------

@router.post("/sandbox/{project_id}/pause")
async def pause_project_sandbox(project_id: str, request: Request):
    """Manually pause a project's sandbox."""
    client = await db.client

    project = await client.table('projects').select('sandbox').eq('project_id', project_id).execute()
    if not project.data:
        raise HTTPException(status_code=404, detail="Project not found")

    raw = project.data[0].get('sandbox', '{}')
    sandbox_info = json.loads(raw) if isinstance(raw, str) else (raw or {})
    sandbox_id = sandbox_info.get('id')
    if not sandbox_id:
        raise HTTPException(status_code=404, detail="No sandbox found for this project")

    if sandbox_info.get('state') == 'paused':
        return {"status": "already_paused", "sandbox_id": sandbox_id}

    paused = await pause_sandbox(sandbox_id)
    if not paused:
        raise HTTPException(status_code=500, detail="Failed to pause sandbox")

    sandbox_info = {
        **sandbox_info,
        'state': 'paused',
        'paused_at': datetime.now(timezone.utc).isoformat(),
    }
    await (
        client.table('projects')
        .eq('project_id', project_id)
        .update({
            'sandbox': json.dumps(sandbox_info)
        })
        .execute()
    )

    return {"status": "paused", "sandbox_id": sandbox_id}


@router.get("/sandbox/{project_id}/state")
async def get_sandbox_state(project_id: str, request: Request):
    """Return sandbox state info for a project."""
    client = await db.client

    project = await client.table('projects').select('sandbox').eq('project_id', project_id).execute()
    if not project.data:
        raise HTTPException(status_code=404, detail="Project not found")

    raw = project.data[0].get('sandbox', '{}')
    sandbox_info = json.loads(raw) if isinstance(raw, str) else (raw or {})

    return {
        "id": sandbox_info.get('id'),
        "state": sandbox_info.get('state', 'unknown'),
        "type": sandbox_info.get('type'),
        "paused_at": sandbox_info.get('paused_at'),
        "created_at": sandbox_info.get('created_at'),
    }


@router.post("/sandbox/renew-expiring")
async def renew_expiring_sandboxes_endpoint(request: Request):
    """Clone-renew sandboxes approaching the 30-day expiry. Designed for crontab trigger."""
    client = await db.client
    max_age_days = getattr(config, 'SANDBOX_CLONE_RENEWAL_DAYS', 29)
    result = await renew_expiring_sandboxes(client, max_age_days=max_age_days)
    return result
