"""Redis-backed sandbox lease helpers for strict Shadow Clone runs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Collection, Dict, List, Optional

from services import redis as redis_service
from utils.logger import logger


LEASE_KEY_TEMPLATE = "shadow_clone:{run_id}:sandbox_lease"
PROJECT_ACTIVE_RUNS_TEMPLATE = "shadow_clone:project:{project_id}:active_sandbox_runs"
PROJECT_LAST_RUN_TEMPLATE = "shadow_clone:project:{project_id}:last_sandbox_run"
LEASE_TTL_SECONDS = int(
    os.getenv(
        "SHADOW_CLONE_SANDBOX_LEASE_TTL_SECONDS",
        str(redis_service.REDIS_KEY_TTL),
    )
)
ACTIVE_BINDING_STATES = frozenset(
    {
        "locked",
        "reattaching",
        "recovery_required",
        "recovering",
    }
)
ATTACHABLE_BINDING_STATES = frozenset(
    {
        "locked",
        "reattaching",
    }
)


def _lease_key(run_id: str) -> str:
    return LEASE_KEY_TEMPLATE.format(run_id=run_id)


def _project_active_runs_key(project_id: str) -> str:
    return PROJECT_ACTIVE_RUNS_TEMPLATE.format(project_id=project_id)


def _project_last_run_key(project_id: str) -> str:
    return PROJECT_LAST_RUN_TEMPLATE.format(project_id=project_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_sandbox_info(value: Any) -> Dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = {}
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if v is not None}


def _normalize_string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []

    normalized: List[str] = []
    seen: set[str] = set()
    for item in value:
        candidate = str(item or "").strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _normalize_checkpoint_layer_index(value: Any, default: int = -1) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _normalize_lease(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Failed to parse Shadow Clone sandbox lease JSON")
            return None
    if not isinstance(raw, dict):
        return None

    run_id = str(raw.get("run_id") or "").strip()
    project_id = str(raw.get("project_id") or "").strip()
    sandbox_id = str(raw.get("sandbox_id") or "").strip()
    if not run_id or not project_id or not sandbox_id:
        return None

    return {
        "run_id": run_id,
        "thread_id": str(raw.get("thread_id") or "").strip(),
        "project_id": project_id,
        "sandbox_id": sandbox_id,
        "sandbox_type": str(raw.get("sandbox_type") or "desktop").strip() or "desktop",
        "sandbox_info": _normalize_sandbox_info(raw.get("sandbox_info")),
        "binding_state": str(raw.get("binding_state") or "locked").strip().lower() or "locked",
        "source": str(raw.get("source") or "").strip(),
        "last_error": str(raw.get("last_error") or "").strip() or None,
        "checkpoint_layer_index": _normalize_checkpoint_layer_index(
            raw.get("checkpoint_layer_index"),
            -1,
        ),
        "checkpoint_subtask_ids": _normalize_string_list(raw.get("checkpoint_subtask_ids")),
        "checkpoint_captured_at": str(raw.get("checkpoint_captured_at") or "").strip() or None,
        "standby_sandbox_id": str(raw.get("standby_sandbox_id") or "").strip() or None,
        "standby_sandbox_type": str(raw.get("standby_sandbox_type") or "").strip() or None,
        "standby_sandbox_info": _normalize_sandbox_info(raw.get("standby_sandbox_info")),
        "standby_snapshot_template_id": (
            str(raw.get("standby_snapshot_template_id") or "").strip() or None
        ),
        "standby_source_sandbox_id": (
            str(raw.get("standby_source_sandbox_id") or "").strip() or None
        ),
        "standby_created_at": str(raw.get("standby_created_at") or "").strip() or None,
        "approved_sandbox_ids": _normalize_string_list(raw.get("approved_sandbox_ids")),
        "last_clone_error": str(raw.get("last_clone_error") or "").strip() or None,
        "execution_epoch": _normalize_checkpoint_layer_index(raw.get("execution_epoch"), 0),
        "lease_epoch": _normalize_checkpoint_layer_index(raw.get("lease_epoch"), 0),
        "environment_status": (
            str(raw.get("environment_status") or "pending").strip().lower() or "pending"
        ),
        "environment_ready": bool(raw.get("environment_ready")),
        "environment_prepared_at": (
            str(raw.get("environment_prepared_at") or "").strip() or None
        ),
        "environment_manifest": _normalize_sandbox_info(raw.get("environment_manifest")),
        "created_at": str(raw.get("created_at") or "").strip() or _now_iso(),
        "updated_at": str(raw.get("updated_at") or "").strip() or _now_iso(),
        "released_at": str(raw.get("released_at") or "").strip() or None,
    }


def _is_active_binding_state(state: str) -> bool:
    return str(state or "").strip().lower() in ACTIVE_BINDING_STATES


def is_active_shadow_clone_binding_state(state: Any) -> bool:
    return _is_active_binding_state(str(state or ""))


def is_attachable_shadow_clone_binding_state(state: Any) -> bool:
    return str(state or "").strip().lower() in ATTACHABLE_BINDING_STATES


def require_shadow_clone_sandbox_lease(
    lease: Optional[Dict[str, Any]],
    *,
    run_id: str,
    expected_project_id: Optional[str] = None,
    require_sandbox_id: bool = False,
    expected_sandbox_id: Optional[str] = None,
    expected_environment_status: Optional[str] = None,
    expected_environment_ready: Optional[bool] = None,
    expected_execution_epoch: Optional[int] = None,
    allowed_binding_states: Optional[Collection[str]] = None,
    require_environment_manifest_consistency: bool = False,
) -> Dict[str, Any]:
    normalized_run_id = str(run_id or "").strip()
    if lease is None:
        raise RuntimeError(
            f"Shadow Clone sandbox lease missing for run {normalized_run_id or '<missing>'}."
        )

    lease_run_id = str(lease.get("run_id") or "").strip()
    if lease_run_id and normalized_run_id and lease_run_id != normalized_run_id:
        raise RuntimeError(
            f"Shadow Clone sandbox lease run mismatch for {normalized_run_id}: {lease_run_id}."
        )

    lease_project_id = str(lease.get("project_id") or "").strip()
    if expected_project_id is not None and lease_project_id != str(expected_project_id or "").strip():
        raise RuntimeError(
            f"Shadow Clone sandbox lease project mismatch for run {normalized_run_id}: "
            f"{lease_project_id or '<missing>'} != {str(expected_project_id or '').strip() or '<missing>'}."
        )

    sandbox_id = str(lease.get("sandbox_id") or "").strip()
    if require_sandbox_id and not sandbox_id:
        raise RuntimeError(
            f"Shadow Clone sandbox lease missing sandbox_id for run {normalized_run_id}."
        )

    if expected_sandbox_id is not None and sandbox_id != str(expected_sandbox_id or "").strip():
        raise RuntimeError(
            f"Shadow Clone sandbox lease sandbox mismatch for run {normalized_run_id}: "
            f"{sandbox_id or '<missing>'} != {str(expected_sandbox_id or '').strip() or '<missing>'}."
        )

    binding_state = str(lease.get("binding_state") or "").strip().lower()
    if allowed_binding_states is not None:
        normalized_allowed_states = {
            str(item or "").strip().lower()
            for item in allowed_binding_states
            if str(item or "").strip()
        }
        if binding_state not in normalized_allowed_states:
            raise RuntimeError(
                f"Shadow Clone sandbox lease binding_state '{binding_state or '<missing>'}' is not allowed "
                f"for run {normalized_run_id}."
            )

    if expected_environment_status is not None:
        actual_environment_status = str(lease.get("environment_status") or "").strip().lower()
        normalized_environment_status = str(expected_environment_status or "").strip().lower()
        if actual_environment_status != normalized_environment_status:
            raise RuntimeError(
                f"Shadow Clone sandbox lease environment_status mismatch for run {normalized_run_id}: "
                f"{actual_environment_status or '<missing>'} != {normalized_environment_status or '<missing>'}."
            )

    if expected_environment_ready is not None:
        actual_environment_ready = bool(lease.get("environment_ready"))
        if actual_environment_ready != bool(expected_environment_ready):
            raise RuntimeError(
                f"Shadow Clone sandbox lease environment_ready mismatch for run {normalized_run_id}: "
                f"{actual_environment_ready} != {bool(expected_environment_ready)}."
            )

    manifest = lease.get("environment_manifest") or {}
    if not isinstance(manifest, dict):
        manifest = {}

    if require_environment_manifest_consistency:
        manifest_sandbox = manifest.get("sandbox") or {}
        if not isinstance(manifest_sandbox, dict):
            manifest_sandbox = {}
        manifest_sandbox_id = str(manifest_sandbox.get("id") or "").strip()
        if manifest_sandbox_id and sandbox_id and manifest_sandbox_id != sandbox_id:
            raise RuntimeError(
                f"Shadow Clone sandbox lease manifest sandbox mismatch for run {normalized_run_id}: "
                f"{manifest_sandbox_id} != {sandbox_id}."
            )

    if expected_execution_epoch is not None:
        normalized_epoch = max(0, int(expected_execution_epoch or 0))
        execution_epoch = _normalize_checkpoint_layer_index(lease.get("execution_epoch"), 0)
        lease_epoch = _normalize_checkpoint_layer_index(lease.get("lease_epoch"), 0)
        if execution_epoch != normalized_epoch:
            raise RuntimeError(
                f"Shadow Clone sandbox lease execution_epoch mismatch for run {normalized_run_id}: "
                f"{execution_epoch} != {normalized_epoch}."
            )
        if lease_epoch != normalized_epoch:
            raise RuntimeError(
                f"Shadow Clone sandbox lease lease_epoch mismatch for run {normalized_run_id}: "
                f"{lease_epoch} != {normalized_epoch}."
            )
        if require_environment_manifest_consistency and manifest and _normalize_checkpoint_layer_index(
            manifest.get("execution_epoch"),
            normalized_epoch,
        ) != normalized_epoch:
            raise RuntimeError(
                f"Shadow Clone sandbox lease manifest execution_epoch mismatch for run {normalized_run_id}."
            )

    return lease


async def get_required_run_sandbox_lease(
    run_id: str,
    **validation_kwargs: Any,
) -> Dict[str, Any]:
    lease = await get_run_sandbox_lease(run_id)
    return require_shadow_clone_sandbox_lease(
        lease,
        run_id=run_id,
        **validation_kwargs,
    )


async def save_run_sandbox_lease(
    run_id: str,
    *,
    project_id: str,
    thread_id: str,
    sandbox_id: str,
    sandbox_type: str,
    sandbox_info: Dict[str, Any],
    binding_state: str = "locked",
    source: str = "",
    last_error: Optional[str] = None,
) -> Dict[str, Any]:
    """Create or update one run-scoped sandbox lease."""
    existing = await get_run_sandbox_lease(run_id)
    now_iso = _now_iso()
    existing_approved_ids = _normalize_string_list(existing.get("approved_sandbox_ids") if existing else [])
    approved_ids = _normalize_string_list(
        [
            *existing_approved_ids,
            str(sandbox_id or "").strip(),
            str(existing.get("standby_sandbox_id") if existing else "").strip(),
        ]
    )
    lease = {
        "run_id": str(run_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
        "project_id": str(project_id or "").strip(),
        "sandbox_id": str(sandbox_id or "").strip(),
        "sandbox_type": str(sandbox_type or "desktop").strip() or "desktop",
        "sandbox_info": {
            **_normalize_sandbox_info(existing.get("sandbox_info") if existing else {}),
            **_normalize_sandbox_info(sandbox_info),
            "id": str(sandbox_id or "").strip(),
            "type": str(sandbox_type or "desktop").strip() or "desktop",
        },
        "binding_state": str(binding_state or "locked").strip().lower() or "locked",
        "source": str(source or (existing.get("source") if existing else "")).strip(),
        "last_error": str(last_error or "").strip() or None,
        "checkpoint_layer_index": int(
            existing.get("checkpoint_layer_index")
            if existing and existing.get("checkpoint_layer_index") is not None
            else -1
        ),
        "checkpoint_subtask_ids": _normalize_string_list(
            existing.get("checkpoint_subtask_ids") if existing else []
        ),
        "checkpoint_captured_at": (
            str(existing.get("checkpoint_captured_at") or "").strip()
            if existing
            else None
        ) or None,
        "standby_sandbox_id": (
            str(existing.get("standby_sandbox_id") or "").strip() if existing else ""
        ) or None,
        "standby_sandbox_type": (
            str(existing.get("standby_sandbox_type") or "").strip() if existing else ""
        ) or None,
        "standby_sandbox_info": _normalize_sandbox_info(
            existing.get("standby_sandbox_info") if existing else {}
        ),
        "standby_snapshot_template_id": (
            str(existing.get("standby_snapshot_template_id") or "").strip()
            if existing
            else ""
        ) or None,
        "standby_source_sandbox_id": (
            str(existing.get("standby_source_sandbox_id") or "").strip()
            if existing
            else ""
        ) or None,
        "standby_created_at": (
            str(existing.get("standby_created_at") or "").strip() if existing else ""
        ) or None,
        "approved_sandbox_ids": approved_ids,
        "last_clone_error": (
            str(existing.get("last_clone_error") or "").strip() if existing else ""
        ) or None,
        "execution_epoch": int(existing.get("execution_epoch") or 0) if existing else 0,
        "lease_epoch": int(existing.get("lease_epoch") or 0) if existing else 0,
        "environment_status": (
            str(existing.get("environment_status") or "").strip().lower()
            if existing
            else "pending"
        ) or "pending",
        "environment_ready": bool(existing.get("environment_ready")) if existing else False,
        "environment_prepared_at": (
            str(existing.get("environment_prepared_at") or "").strip() if existing else ""
        ) or None,
        "environment_manifest": _normalize_sandbox_info(
            existing.get("environment_manifest") if existing else {}
        ),
        "created_at": str(existing.get("created_at") if existing else "") or now_iso,
        "updated_at": now_iso,
        "released_at": None,
    }
    if lease["binding_state"] not in ACTIVE_BINDING_STATES:
        lease["released_at"] = now_iso
    elif existing:
        lease["released_at"] = existing.get("released_at")

    return await _persist_run_sandbox_lease(lease)


async def _persist_run_sandbox_lease(lease: Dict[str, Any]) -> Dict[str, Any]:
    run_id = str(lease.get("run_id") or "").strip()
    project_id = str(lease.get("project_id") or "").strip()
    lease_key = _lease_key(run_id)
    project_last_run_key = _project_last_run_key(project_id)
    project_active_runs_key = _project_active_runs_key(project_id)
    lease_payload = json.dumps(lease, ensure_ascii=False)
    redis_client = await redis_service.get_client()

    async def _execute_pipeline() -> None:
        pipeline = redis_client.pipeline(transaction=True)
        pipeline.set(
            lease_key,
            lease_payload,
            ex=LEASE_TTL_SECONDS,
        )
        pipeline.set(
            project_last_run_key,
            str(run_id),
            ex=LEASE_TTL_SECONDS,
        )
        pipeline.expire(project_active_runs_key, LEASE_TTL_SECONDS)
        if _is_active_binding_state(str(lease.get("binding_state") or "")):
            pipeline.sadd(project_active_runs_key, str(run_id))
        else:
            pipeline.srem(project_active_runs_key, str(run_id))
        await pipeline.execute()

    run_redis_operation = getattr(redis_service, "_run_redis_operation", None)
    if callable(run_redis_operation):
        await run_redis_operation(
            "shadow_clone.persist_sandbox_lease",
            _execute_pipeline,
        )
    else:
        await _execute_pipeline()
    return lease


async def get_run_sandbox_lease(run_id: str) -> Optional[Dict[str, Any]]:
    raw = await redis_service.get(_lease_key(run_id))
    return _normalize_lease(raw)


async def update_run_sandbox_lease(
    run_id: str,
    **patch: Any,
) -> Optional[Dict[str, Any]]:
    existing = await get_run_sandbox_lease(run_id)
    if existing is None:
        return None

    now_iso = _now_iso()
    merged = {
        **existing,
        **patch,
        "run_id": existing["run_id"],
        "project_id": existing["project_id"],
        "thread_id": str(
            patch.get("thread_id", existing.get("thread_id") or "")
        ).strip(),
        "sandbox_id": str(
            patch.get("sandbox_id", existing.get("sandbox_id") or "")
        ).strip(),
        "sandbox_type": str(
            patch.get("sandbox_type", existing.get("sandbox_type") or "desktop")
        ).strip()
        or "desktop",
        "sandbox_info": _normalize_sandbox_info(
            patch.get("sandbox_info", existing.get("sandbox_info") or {})
        ),
        "binding_state": str(
            patch.get("binding_state", existing.get("binding_state") or "locked")
        ).strip()
        .lower()
        or "locked",
        "source": str(patch.get("source", existing.get("source") or "")).strip(),
        "last_error": (
            str(patch.get("last_error") or "").strip()
            if "last_error" in patch
            else str(existing.get("last_error") or "").strip()
        )
        or None,
        "checkpoint_layer_index": _normalize_checkpoint_layer_index(
            patch["checkpoint_layer_index"]
            if "checkpoint_layer_index" in patch
            else existing.get("checkpoint_layer_index"),
            -1,
        ),
        "checkpoint_subtask_ids": _normalize_string_list(
            patch.get("checkpoint_subtask_ids", existing.get("checkpoint_subtask_ids") or [])
        ),
        "checkpoint_captured_at": (
            str(
                patch.get("checkpoint_captured_at", existing.get("checkpoint_captured_at") or "")
            ).strip()
            or None
        ),
        "standby_sandbox_id": (
            str(patch.get("standby_sandbox_id") or "").strip()
            if "standby_sandbox_id" in patch
            else existing.get("standby_sandbox_id")
        )
        or None,
        "standby_sandbox_type": (
            str(patch.get("standby_sandbox_type") or "").strip()
            if "standby_sandbox_type" in patch
            else existing.get("standby_sandbox_type")
        )
        or None,
        "standby_sandbox_info": _normalize_sandbox_info(
            patch.get("standby_sandbox_info", existing.get("standby_sandbox_info") or {})
        ),
        "standby_snapshot_template_id": (
            str(patch.get("standby_snapshot_template_id") or "").strip()
            if "standby_snapshot_template_id" in patch
            else str(existing.get("standby_snapshot_template_id") or "").strip()
        )
        or None,
        "standby_source_sandbox_id": (
            str(patch.get("standby_source_sandbox_id") or "").strip()
            if "standby_source_sandbox_id" in patch
            else str(existing.get("standby_source_sandbox_id") or "").strip()
        )
        or None,
        "standby_created_at": (
            str(patch.get("standby_created_at") or "").strip()
            if "standby_created_at" in patch
            else str(existing.get("standby_created_at") or "").strip()
        )
        or None,
        "approved_sandbox_ids": _normalize_string_list(
            patch.get(
                "approved_sandbox_ids",
                [
                    *(existing.get("approved_sandbox_ids") or []),
                    patch.get("sandbox_id", existing.get("sandbox_id") or ""),
                    patch.get("standby_sandbox_id", existing.get("standby_sandbox_id") or ""),
                ],
            )
        ),
        "last_clone_error": (
            str(patch.get("last_clone_error") or "").strip()
            if "last_clone_error" in patch
            else str(existing.get("last_clone_error") or "").strip()
        )
        or None,
        "execution_epoch": _normalize_checkpoint_layer_index(
            patch["execution_epoch"]
            if "execution_epoch" in patch
            else existing.get("execution_epoch"),
            0,
        ),
        "lease_epoch": _normalize_checkpoint_layer_index(
            patch["lease_epoch"]
            if "lease_epoch" in patch
            else existing.get("lease_epoch"),
            0,
        ),
        "environment_status": str(
            patch.get("environment_status", existing.get("environment_status") or "pending")
        ).strip().lower()
        or "pending",
        "environment_ready": (
            bool(patch.get("environment_ready"))
            if "environment_ready" in patch
            else bool(existing.get("environment_ready"))
        ),
        "environment_prepared_at": (
            str(patch.get("environment_prepared_at") or "").strip()
            if "environment_prepared_at" in patch
            else str(existing.get("environment_prepared_at") or "").strip()
        )
        or None,
        "environment_manifest": {
            **_normalize_sandbox_info(existing.get("environment_manifest") or {}),
            **_normalize_sandbox_info(patch.get("environment_manifest") or {}),
        },
        "created_at": str(existing.get("created_at") or now_iso),
        "updated_at": now_iso,
        "released_at": (
            now_iso
            if str(patch.get("binding_state", existing.get("binding_state") or "")).strip().lower()
            not in ACTIVE_BINDING_STATES
            else existing.get("released_at")
        ),
    }

    if not merged["standby_sandbox_id"]:
        merged["standby_sandbox_type"] = None
        merged["standby_sandbox_info"] = {}
        merged["standby_snapshot_template_id"] = None
        merged["standby_source_sandbox_id"] = None
        merged["standby_created_at"] = None

    return await _persist_run_sandbox_lease(merged)


async def set_run_sandbox_binding_state(
    run_id: str,
    binding_state: str,
    *,
    last_error: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    return await update_run_sandbox_lease(
        run_id,
        binding_state=binding_state,
        last_error=last_error,
    )


async def mark_run_sandbox_unattachable(
    run_id: str,
    *,
    expected_sandbox_id: Optional[str] = None,
    last_error: Optional[str] = None,
    standby_available: Optional[bool] = None,
) -> Optional[Dict[str, Any]]:
    existing = await get_run_sandbox_lease(run_id)
    if existing is None:
        return None

    current_sandbox_id = str(existing.get("sandbox_id") or "").strip()
    expected_id = str(expected_sandbox_id or "").strip()
    if expected_id and current_sandbox_id != expected_id:
        return existing

    current_binding_state = str(existing.get("binding_state") or "").strip().lower()
    has_standby = (
        bool(standby_available)
        if standby_available is not None
        else bool(str(existing.get("standby_sandbox_id") or "").strip())
    )

    if current_binding_state == "recovering":
        target_binding_state = "recovering"
    elif current_binding_state == "lost":
        target_binding_state = "lost"
    elif current_binding_state == "recovery_required":
        target_binding_state = "recovery_required"
    else:
        target_binding_state = "recovery_required" if has_standby else "lost"

    target_environment_status = (
        "recovering"
        if target_binding_state in {"recovery_required", "recovering"}
        else "failed"
    )
    return await update_run_sandbox_lease(
        run_id,
        binding_state=target_binding_state,
        environment_status=target_environment_status,
        environment_ready=False,
        last_error=last_error,
    )


async def sync_run_sandbox_execution_epoch(
    run_id: str,
    execution_epoch: int,
) -> Optional[Dict[str, Any]]:
    normalized_epoch = max(0, int(execution_epoch or 0))
    return await update_run_sandbox_lease(
        run_id,
        execution_epoch=normalized_epoch,
        lease_epoch=normalized_epoch,
    )


async def keep_run_sandbox_lease_alive(run_id: str) -> Optional[Dict[str, Any]]:
    return await update_run_sandbox_lease(run_id)


async def get_project_shadow_clone_leases(
    project_id: str,
    *,
    include_recent: bool = False,
) -> List[Dict[str, Any]]:
    leases: List[Dict[str, Any]] = []
    seen_run_ids: set[str] = set()

    active_run_ids = await redis_service.smembers(_project_active_runs_key(project_id))
    for run_id in active_run_ids or []:
        run_id = str(run_id or "").strip()
        if not run_id or run_id in seen_run_ids:
            continue
        lease = await get_run_sandbox_lease(run_id)
        if lease is None or lease.get("project_id") != project_id:
            await redis_service.srem(_project_active_runs_key(project_id), run_id)
            continue
        seen_run_ids.add(run_id)
        leases.append(lease)

    if include_recent:
        last_run_id = str(
            await redis_service.get(_project_last_run_key(project_id), default="") or ""
        ).strip()
        if last_run_id and last_run_id not in seen_run_ids:
            lease = await get_run_sandbox_lease(last_run_id)
            if lease and lease.get("project_id") == project_id:
                leases.append(lease)

    leases.sort(
        key=lambda item: (
            1 if _is_active_binding_state(item.get("binding_state", "")) else 0,
            str(item.get("updated_at") or ""),
        ),
        reverse=True,
    )
    return leases


async def get_preferred_project_sandbox_lease(
    project_id: str,
    *,
    requested_sandbox_id: Optional[str] = None,
    include_recent: bool = False,
) -> Optional[Dict[str, Any]]:
    requested = str(requested_sandbox_id or "").strip()
    leases = await get_project_shadow_clone_leases(
        project_id,
        include_recent=include_recent,
    )
    if not leases:
        return None

    if requested:
        for lease in leases:
            if str(lease.get("sandbox_id") or "").strip() == requested:
                return lease

    active_leases = [
        lease for lease in leases
        if _is_active_binding_state(lease.get("binding_state", ""))
    ]
    if len(active_leases) == 1:
        return active_leases[0]

    if include_recent and len(leases) == 1:
        recent_lease = leases[0]
        if not requested or str(recent_lease.get("sandbox_id") or "").strip() == requested:
            return recent_lease
    return None
