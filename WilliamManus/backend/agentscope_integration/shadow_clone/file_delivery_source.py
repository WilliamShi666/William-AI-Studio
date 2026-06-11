from __future__ import annotations

from typing import Any, Optional


_DEFAULT_ROOT_PATH = "/workspace"
_TERMINAL_PARENT_STATUS_MAP = {
    "completed": "completed",
    "failed": "failed",
    "stopped": "cancelled",
    "cancelled": "cancelled",
}
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def _normalize_optional_string(value: Any) -> Optional[str]:
    normalized = str(value or "").strip()
    return normalized or None


def _extract_manifest_sandbox_id(lease: dict[str, Any]) -> Optional[str]:
    manifest = lease.get("environment_manifest")
    if not isinstance(manifest, dict):
        manifest = lease
    if not isinstance(manifest, dict):
        return None
    sandbox = manifest.get("sandbox")
    if not isinstance(sandbox, dict):
        return None
    return _normalize_optional_string(sandbox.get("id"))


def _extract_state_manifest(state: dict[str, Any]) -> dict[str, Any]:
    environment = state.get("environment")
    if not isinstance(environment, dict):
        return {}
    manifest = environment.get("manifest")
    if not isinstance(manifest, dict):
        return {}
    return manifest


def _resolve_terminal_status(
    *,
    parent_status: Optional[str],
    effective_status: Optional[str],
) -> Optional[str]:
    normalized_effective_status = str(effective_status or "").strip().lower()
    if normalized_effective_status in _TERMINAL_STATUSES:
        return normalized_effective_status

    normalized_parent_status = str(parent_status or "").strip().lower()
    return _TERMINAL_PARENT_STATUS_MAP.get(normalized_parent_status)


def build_run_file_delivery_source(
    *,
    agent_run_id: str,
    parent_status: Optional[str],
    lease: Optional[dict[str, Any]],
    state: Optional[dict[str, Any]] = None,
    effective_status: Optional[str] = None,
    default_root_path: str = _DEFAULT_ROOT_PATH,
) -> dict[str, Any]:
    normalized_lease = lease if isinstance(lease, dict) else {}
    normalized_state = state if isinstance(state, dict) else {}
    state_sandbox = (
        normalized_state.get("sandbox")
        if isinstance(normalized_state.get("sandbox"), dict)
        else {}
    )
    state_environment = (
        normalized_state.get("environment")
        if isinstance(normalized_state.get("environment"), dict)
        else {}
    )
    lease_sandbox_id = _normalize_optional_string(normalized_lease.get("sandbox_id"))
    state_sandbox_id = _normalize_optional_string(state_sandbox.get("id"))
    manifest_sandbox_id = (
        _extract_manifest_sandbox_id(normalized_lease)
        or _extract_manifest_sandbox_id(_extract_state_manifest(normalized_state))
    )
    canonical_sandbox_id = lease_sandbox_id or state_sandbox_id or manifest_sandbox_id
    environment_ready = normalized_lease.get("environment_ready")
    if not isinstance(environment_ready, bool):
        state_environment_ready = state_sandbox.get("environment_ready")
        if isinstance(state_environment_ready, bool):
            environment_ready = state_environment_ready
        else:
            environment_ready = state_environment.get("ready")

    return {
        "agent_run_id": str(agent_run_id).strip(),
        "browse_sandbox_id": canonical_sandbox_id,
        "archive_sandbox_id": canonical_sandbox_id,
        "browse_root_path": default_root_path,
        "archive_root_path": default_root_path,
        "identity_source": (
            "shadow_clone_lease"
            if lease_sandbox_id is not None or _extract_manifest_sandbox_id(normalized_lease) is not None
            else "shadow_clone_state"
            if canonical_sandbox_id is not None
            else "unavailable"
        ),
        "binding_state": _normalize_optional_string(
            normalized_lease.get("binding_state") or state_sandbox.get("binding_state")
        ),
        "environment_status": _normalize_optional_string(
            normalized_lease.get("environment_status")
            or state_sandbox.get("environment_status")
            or state_environment.get("status")
        ),
        "environment_ready": (
            environment_ready if isinstance(environment_ready, bool) else None
        ),
        "execution_epoch": (
            normalized_lease.get("execution_epoch")
            if normalized_lease.get("execution_epoch") is not None
            else normalized_state.get("execution_epoch")
        ),
        "manifest_sandbox_id": manifest_sandbox_id,
        "terminal_status": _resolve_terminal_status(
            parent_status=parent_status,
            effective_status=effective_status,
        ),
    }
