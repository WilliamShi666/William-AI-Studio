from types import SimpleNamespace

import pytest

import agentscope_integration.shadow_clone.sandbox_lease as sandbox_lease
from sandbox import api as sandbox_api
from sandbox import sandbox as sandbox_core
import sandbox.tool_base as tool_base_module
from sandbox.tool_base import SandboxToolsBase


class _DummyStrictTool(SandboxToolsBase):
    pass


@pytest.mark.asyncio
async def test_attach_shadow_clone_bound_sandbox_rejects_non_attachable_lease_before_connect(
    monkeypatch,
):
    lease = {
        "run_id": "run-shadow",
        "project_id": "project-shadow",
        "thread_id": "thread-shadow",
        "sandbox_id": "sb-active",
        "sandbox_type": "code",
        "sandbox_info": {"id": "sb-active", "type": "code", "state": "running"},
        "binding_state": "locked",
    }

    async def _get_lease(_run_id: str):
        return {
            **lease,
            "binding_state": "recovery_required",
            "last_error": "quota exhausted; standby promotion required",
        }

    async def _connect(*_args, **_kwargs):
        raise AssertionError("connect should not run for non-attachable strict leases")

    monkeypatch.setattr(sandbox_api, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(sandbox_api, "_connect_sandbox_with_retry", _connect)

    with pytest.raises(sandbox_api.ShadowCloneStrictSandboxError) as exc_info:
        await sandbox_api.attach_shadow_clone_bound_sandbox(object(), lease=lease)

    assert exc_info.value.error_code == "SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED"
    assert exc_info.value.binding_state == "recovery_required"
    assert exc_info.value.root_cause_code == "SHADOW_CLONE_SANDBOX_LEASE_NOT_ATTACHABLE"


@pytest.mark.asyncio
async def test_resume_or_create_sandbox_short_circuits_nonretryable_provider_resume_failure(
    monkeypatch,
):
    provider_error = sandbox_core.SandboxProviderFailure(
        operation="resume",
        detail="Sandbox provider resume failed for sb-old (code): BALANCE_NOT_ENOUGH: balance not enough",
        root_cause_code="SANDBOX_PROVIDER_BALANCE_NOT_ENOUGH",
        retryable=False,
        sandbox_id="sb-old",
        sandbox_type="code",
        provider_reason="BALANCE_NOT_ENOUGH",
        provider_http_status=402,
        provider_code=400,
    )

    async def _attempt_resume(*_args, **_kwargs):
        return False, provider_error

    async def _get_or_start(*_args, **_kwargs):
        raise AssertionError("reattach attempts should stop after a decisive provider failure")

    monkeypatch.setattr(sandbox_core, "_attempt_ppio_resume", _attempt_resume)
    monkeypatch.setattr(sandbox_core, "get_or_start_sandbox", _get_or_start)

    with pytest.raises(sandbox_core.SandboxOriginalReuseExhausted) as exc_info:
        await sandbox_core.resume_or_create_sandbox(
            password="pw",
            project_id="project-shadow",
            sandbox_type="code",
            sandbox_info={"id": "sb-old", "type": "code", "state": "paused"},
            allow_create=False,
        )

    assert exc_info.value.last_error is provider_error


@pytest.mark.asyncio
async def test_resume_or_create_sandbox_retryable_provider_attach_failure_respects_allow_create_false(
    monkeypatch,
):
    monotonic_values = [0.0, 0.0, 0.02]
    create_calls = {"count": 0}

    async def _attempt_resume(*_args, **_kwargs):
        return False, None

    async def _get_or_start(*_args, **_kwargs):
        raise RuntimeError("429: Rate limit exceeded, please try again later.")

    async def _should_not_create(*_args, **_kwargs):
        create_calls["count"] += 1
        raise AssertionError("create_sandbox must not run when allow_create is False")

    async def _sleep(_seconds: float):
        return None

    monkeypatch.setattr(sandbox_core, "_attempt_ppio_resume", _attempt_resume)
    monkeypatch.setattr(sandbox_core, "get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr(sandbox_core, "create_sandbox", _should_not_create)
    monkeypatch.setattr(sandbox_core.asyncio, "sleep", _sleep)
    monkeypatch.setattr(
        sandbox_core.time,
        "monotonic",
        lambda: monotonic_values.pop(0) if monotonic_values else 0.02,
    )

    with pytest.raises(sandbox_core.SandboxOriginalReuseExhausted) as exc_info:
        await sandbox_core.resume_or_create_sandbox(
            password="pw",
            project_id="project-shadow",
            sandbox_type="code",
            sandbox_info={"id": "sb-old", "type": "code", "state": "running"},
            reattach_budget_seconds=0.01,
            allow_create=False,
        )

    assert create_calls["count"] == 0
    assert exc_info.value.sandbox_id == "sb-old"
    assert isinstance(exc_info.value.last_error, sandbox_core.SandboxProviderFailure)
    assert exc_info.value.last_error.retryable is True
    assert exc_info.value.last_error.provider_reason == "RATE_LIMITED"


@pytest.mark.asyncio
async def test_cached_strict_runtime_sandbox_fails_fast_when_lease_is_non_attachable(
    monkeypatch,
):
    tool = _DummyStrictTool(
        project_id="project-shadow",
        thread_manager=SimpleNamespace(),
        sandbox_type="code",
        shadow_clone_run_id="run-shadow",
        strict_sandbox=True,
    )
    tool._bind_runtime_state(
        sandbox=object(),
        sandbox_id="sb-active",
        sandbox_type="code",
    )

    async def _get_lease(_run_id: str):
        return {
            "run_id": "run-shadow",
            "project_id": "project-shadow",
            "sandbox_id": "sb-active",
            "sandbox_type": "code",
            "binding_state": "lost",
            "last_error": "quota exhausted",
        }

    async def _should_not_attach(_client):
        raise AssertionError("strict attach should not run after a decisive lease failure")

    monkeypatch.setattr(sandbox_lease, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(tool, "_ensure_shadow_clone_bound_sandbox", _should_not_attach)

    with pytest.raises(sandbox_api.ShadowCloneStrictSandboxError) as exc_info:
        await tool._ensure_sandbox()

    assert exc_info.value.error_code == "SHADOW_CLONE_SANDBOX_REATTACH_FAILED"
    assert exc_info.value.binding_state == "lost"
    assert exc_info.value.root_cause_code == "SHADOW_CLONE_SANDBOX_LEASE_NOT_ATTACHABLE"
    assert tool._get_runtime_sandbox() is None


@pytest.mark.asyncio
async def test_cached_strict_runtime_sandbox_waits_for_transient_missing_lease(
    monkeypatch,
):
    tool = _DummyStrictTool(
        project_id="project-shadow",
        thread_manager=SimpleNamespace(),
        sandbox_type="code",
        shadow_clone_run_id="run-shadow",
        strict_sandbox=True,
    )
    runtime_sandbox = object()
    tool._bind_runtime_state(
        sandbox=runtime_sandbox,
        sandbox_id="sb-active",
        sandbox_type="code",
    )

    lease_reads = {"count": 0}

    async def _get_lease(_run_id: str):
        lease_reads["count"] += 1
        if lease_reads["count"] == 1:
            return None
        return {
            "run_id": "run-shadow",
            "project_id": "project-shadow",
            "sandbox_id": "sb-active",
            "sandbox_type": "code",
            "sandbox_info": {"id": "sb-active", "type": "code"},
            "binding_state": "locked",
        }

    async def _sleep(_seconds: float):
        return None

    async def _should_not_attach(_client):
        raise AssertionError("cached strict sandbox should not reattach after transient lease miss")

    monkeypatch.setattr(sandbox_lease, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(tool_base_module, "SHADOW_CLONE_LEASE_WAIT_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(tool_base_module, "SHADOW_CLONE_LEASE_WAIT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(tool_base_module.asyncio, "sleep", _sleep)
    monkeypatch.setattr(tool, "_ensure_shadow_clone_bound_sandbox", _should_not_attach)

    result = await tool._ensure_sandbox()

    assert result is runtime_sandbox
    assert lease_reads["count"] >= 2
    assert tool._get_runtime_sandbox() is runtime_sandbox
