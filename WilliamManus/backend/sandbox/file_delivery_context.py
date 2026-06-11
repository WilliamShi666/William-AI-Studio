from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional

from fastapi import HTTPException

from services.workspace_artifacts import workspace_artifacts
from utils.logger import logger


VerifySandboxAccess = Callable[..., Awaitable[Dict[str, Any]]]
FailureDetailBuilder = Callable[..., Dict[str, Any]]
PreferredLeaseResolver = Callable[..., Awaitable[Optional[Dict[str, Any]]]]


@dataclass(frozen=True)
class FileDeliveryContext:
    project_data: Dict[str, Any]
    project_id: str
    project_sandbox_info: Dict[str, Any]
    project_sandbox_id: Optional[str]
    identity_source: str
    shadow_clone_binding_state: Optional[str]


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


def _verify_project_access_by_row(
    project_data: Dict[str, Any],
    user_id: Optional[str] = None,
) -> Dict[str, Any]:
    if project_data.get("is_public"):
        return project_data

    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required for this resource")

    account_id = project_data.get("account_id")
    if account_id and account_id == user_id:
        return project_data

    raise HTTPException(status_code=403, detail="Not authorized to access this sandbox")


async def _load_project_by_id(client: Any, project_id: str) -> Dict[str, Any]:
    project_result = await (
        client.table("projects")
        .select("*")
        .eq("project_id", project_id)
        .execute()
    )
    if not project_result.data or len(project_result.data) == 0:
        raise HTTPException(status_code=404, detail="Project not found")
    return project_result.data[0]


def _build_default_identity_failure_detail(
    *,
    requested_sandbox_id: str,
    resolved_sandbox_id: str,
    original_sandbox_id: Optional[str],
    artifact_store_state: str,
    error_code: str = "FILE_PROJECT_CONTEXT_NOT_FOUND",
    message: str = (
        "File request could not be mapped to a project sandbox context. "
        "Durable artifact lookup could not safely recover the project."
    ),
) -> Dict[str, Any]:
    return {
        "message": message,
        "error_code": error_code,
        "requested_sandbox_id": requested_sandbox_id,
        "resolved_sandbox_id": resolved_sandbox_id,
        "original_sandbox_id": original_sandbox_id,
        "artifact_store_state": artifact_store_state,
    }


async def resolve_file_delivery_context(
    *,
    client: Any,
    requested_sandbox_id: str,
    resolved_sandbox_id: str,
    original_sandbox_id: Optional[str],
    user_id: Optional[str],
    verify_sandbox_access: VerifySandboxAccess,
    preferred_lease_resolver: Optional[PreferredLeaseResolver] = None,
    failure_detail_builder: Optional[FailureDetailBuilder] = None,
) -> FileDeliveryContext:
    try:
        project_data = await verify_sandbox_access(
            client,
            resolved_sandbox_id,
            user_id,
            original_sandbox_id=original_sandbox_id,
        )
        identity_source = "sandbox_access"
    except HTTPException as access_error:
        if access_error.status_code != 404:
            raise

        artifact_hint = None
        candidate_sandbox_ids = []
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
                "File delivery artifact-based project lookup failed",
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
            detail = (
                failure_detail_builder(
                    requested_sandbox_id=requested_sandbox_id,
                    resolved_sandbox_id=resolved_sandbox_id,
                    original_sandbox_id=original_sandbox_id,
                    artifact_store_state=workspace_artifacts.availability_state(),
                )
                if failure_detail_builder is not None
                else _build_default_identity_failure_detail(
                    requested_sandbox_id=requested_sandbox_id,
                    resolved_sandbox_id=resolved_sandbox_id,
                    original_sandbox_id=original_sandbox_id,
                    artifact_store_state=workspace_artifacts.availability_state(),
                )
            )
            logger.warning("File delivery project context resolution failed", extra=detail)
            raise HTTPException(status_code=404, detail=detail)

        project_data = await _load_project_by_id(client, artifact_hint["project_id"])
        _verify_project_access_by_row(project_data, user_id)
        identity_source = "artifact_hint"

        logger.info(
            "File delivery context resolved via workspace artifacts",
            extra={
                "requested_sandbox_id": requested_sandbox_id,
                "resolved_sandbox_id": resolved_sandbox_id,
                "original_sandbox_id": original_sandbox_id,
                "artifact_hint_sandbox_id": artifact_hint.get("sandbox_id"),
                "project_id": artifact_hint.get("project_id"),
                "artifact_store_state": workspace_artifacts.availability_state(),
            },
        )

    project_id = str(project_data.get("project_id") or "").strip()
    if not project_id:
        detail = _build_default_identity_failure_detail(
            requested_sandbox_id=requested_sandbox_id,
            resolved_sandbox_id=resolved_sandbox_id,
            original_sandbox_id=original_sandbox_id,
            artifact_store_state=workspace_artifacts.availability_state(),
            error_code="FILE_PROJECT_CONTEXT_INVALID",
            message="File request resolved to a project row without a project identifier.",
        )
        logger.warning("File delivery context resolved without project id", extra=detail)
        raise HTTPException(status_code=404, detail=detail)

    project_sandbox_info = _parse_sandbox_info(project_data.get("sandbox", {}))
    project_sandbox_id = str(
        project_sandbox_info.get("id")
        or resolved_sandbox_id
        or requested_sandbox_id
        or ""
    ).strip() or None
    shadow_clone_binding_state: Optional[str] = None

    if (
        preferred_lease_resolver is not None
        and project_id
        and project_sandbox_id
    ):
        try:
            preferred_lease = await preferred_lease_resolver(
                project_id,
                requested_sandbox_id=project_sandbox_id,
                include_recent=True,
            )
        except Exception as lease_error:
            logger.debug(
                "File delivery preferred lease lookup failed",
                extra={
                    "requested_sandbox_id": requested_sandbox_id,
                    "resolved_sandbox_id": resolved_sandbox_id,
                    "original_sandbox_id": original_sandbox_id,
                    "project_id": project_id,
                    "project_sandbox_id": project_sandbox_id,
                    "error": str(lease_error),
                },
            )
        else:
            shadow_clone_binding_state = (
                str(preferred_lease.get("binding_state") or "").strip().lower() or None
                if isinstance(preferred_lease, dict)
                else None
            )

    return FileDeliveryContext(
        project_data=project_data,
        project_id=project_id,
        project_sandbox_info=project_sandbox_info,
        project_sandbox_id=project_sandbox_id,
        identity_source=identity_source,
        shadow_clone_binding_state=shadow_clone_binding_state,
    )
