"""Unit tests for sandbox/sandbox.py – pause_sandbox, clone_sandbox, resume_or_create_sandbox."""

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# Lightweight stubs so sandbox.sandbox can be imported without real SDKs
# ---------------------------------------------------------------------------

# Fake ppio_sandbox.core
_fake_ppio = types.ModuleType("ppio_sandbox")
_fake_ppio_core = types.ModuleType("ppio_sandbox.core")


class _FakePPIOSandbox:
    """Minimal stand-in for ppio_sandbox.core.Sandbox."""

    _cls_pause_calls: list = []
    _cls_resume_calls: list = []
    _clone_calls: list = []
    _clone_result = None
    _cls_pause_error = None
    _cls_resume_error = None
    _clone_error = None

    @classmethod
    def reset(cls):
        cls._cls_pause_calls = []
        cls._cls_resume_calls = []
        cls._clone_calls = []
        cls._clone_result = None
        cls._cls_pause_error = None
        cls._cls_resume_error = None
        cls._clone_error = None

    @classmethod
    def _cls_pause(cls, sandbox_id: str):
        if cls._cls_pause_error:
            raise cls._cls_pause_error
        cls._cls_pause_calls.append(sandbox_id)

    @classmethod
    def _cls_resume(cls, sandbox_id: str, timeout: int = 3600, **_kwargs):
        if cls._cls_resume_error:
            raise cls._cls_resume_error
        cls._cls_resume_calls.append((sandbox_id, timeout))
        return True

    @classmethod
    def clone(cls, sandbox_id: str, count: int, *, timeout: int = 3600):
        if cls._clone_error:
            raise cls._clone_error
        cls._clone_calls.append((sandbox_id, count, timeout))
        return cls._clone_result


_fake_ppio_core.Sandbox = _FakePPIOSandbox
_fake_ppio.core = _fake_ppio_core
sys.modules.setdefault("ppio_sandbox", _fake_ppio)
sys.modules.setdefault("ppio_sandbox.core", _fake_ppio_core)

# Ensure structlog stub exists (needed by utils.logger)
if "structlog" not in sys.modules:
    from pathlib import Path
    _backend = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(_backend))

    class _DummyBoundLogger:
        def info(self, *a, **kw): return None
        def warning(self, *a, **kw): return None
        def error(self, *a, **kw): return None
        def debug(self, *a, **kw): return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*a, **kw): return None

    sys.modules["structlog"] = types.SimpleNamespace(
        configure=lambda **kw: None,
        get_logger=lambda *a, **kw: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *a, **kw: None,
            PositionalArgumentsFormatter=lambda *a, **kw: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *a, **kw: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *a, **kw: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kw: None,
            get_contextvars=lambda: {},
        ),
    )

from sandbox.sandbox import (
    pause_sandbox,
    clone_sandbox,
    classify_sandbox_provider_failure,
    create_sandbox,
    resume_or_create_sandbox,
    SandboxOriginalReuseExhausted,
    SandboxProviderFailure,
    get_or_start_sandbox,
)
import sandbox.sandbox as sandbox_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeSandbox:
    def __init__(self, sandbox_id: str, *, running: bool = True):
        self.sandbox_id = sandbox_id
        self._running = running

    def is_running(self):
        return self._running


@pytest.fixture(autouse=True)
def _reset_ppio():
    _FakePPIOSandbox.reset()
    yield


@pytest.fixture(autouse=True)
def _fast_reattach_budget(monkeypatch):
    monkeypatch.setattr(sandbox_module.config, "SANDBOX_REATTACH_BUDGET_SECONDS", 0.01, raising=False)
    monkeypatch.setattr(sandbox_module.config, "SANDBOX_REATTACH_PROBE_TIMEOUT_SECONDS", 0.1, raising=False)


@pytest.fixture(autouse=True)
def _inline_to_thread(monkeypatch):
    async def _run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("sandbox.sandbox.asyncio.to_thread", _run_inline)


# ---------------------------------------------------------------------------
# delete_sandbox
# ---------------------------------------------------------------------------

def test_is_sandbox_not_found_error_patterns():
    assert sandbox_module._is_sandbox_not_found_error(RuntimeError("Sandbox not found"))
    assert sandbox_module._is_sandbox_not_found_error(RuntimeError("404 resource missing"))
    assert not sandbox_module._is_sandbox_not_found_error(RuntimeError("permission denied"))


def test_classify_sandbox_provider_failure_recognizes_plain_http_429():
    failure = classify_sandbox_provider_failure(
        RuntimeError("429: Rate limit exceeded, please try again later."),
        operation="create",
        sandbox_type="code",
    )

    assert isinstance(failure, SandboxProviderFailure)
    assert failure.retryable is True
    assert failure.provider_http_status == 429
    assert failure.provider_reason == "RATE_LIMITED"
    assert failure.root_cause_code == "SANDBOX_PROVIDER_RATE_LIMITED"


def test_classify_sandbox_provider_failure_recognizes_rate_limit_exception_type():
    from e2b import exceptions as e2b_exceptions

    failure = classify_sandbox_provider_failure(
        e2b_exceptions.RateLimitException("Rate limit exceeded, please try again later."),
        operation="create",
        sandbox_type="code",
    )

    assert isinstance(failure, SandboxProviderFailure)
    assert failure.retryable is True
    assert failure.provider_reason == "RATE_LIMITED"
    assert failure.root_cause_code == "SANDBOX_PROVIDER_RATE_LIMITED"


def test_classify_sandbox_provider_failure_ignores_four_digit_prefixes():
    failure = classify_sandbox_provider_failure(
        RuntimeError("5001 websocket closed before sandbox create"),
        operation="create",
        sandbox_type="code",
    )

    assert failure is None


# ---------------------------------------------------------------------------
# pause_sandbox
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_sandbox_success():
    result = await pause_sandbox("sb-1")
    assert result is True
    assert _FakePPIOSandbox._cls_pause_calls == ["sb-1"]


@pytest.mark.asyncio
async def test_pause_sandbox_exception_returns_false():
    _FakePPIOSandbox._cls_pause_error = RuntimeError("sdk boom")
    result = await pause_sandbox("sb-fail")
    assert result is False


# ---------------------------------------------------------------------------
# clone_sandbox
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clone_sandbox_success():
    _FakePPIOSandbox._clone_result = SimpleNamespace(
        sandboxes=[SimpleNamespace(sandbox_id="sb-clone-1")],
        snapshot_template_id="tmpl-abc",
    )
    new_id, tmpl_id = await clone_sandbox("sb-old")
    assert new_id == "sb-clone-1"
    assert tmpl_id == "tmpl-abc"


@pytest.mark.asyncio
async def test_clone_sandbox_propagates_exception():
    _FakePPIOSandbox._clone_error = RuntimeError("clone failed")
    with pytest.raises(RuntimeError, match="clone failed"):
        await clone_sandbox("sb-old")


@pytest.mark.asyncio
async def test_clone_sandbox_passes_count_and_timeout():
    _FakePPIOSandbox._clone_result = SimpleNamespace(
        sandboxes=[SimpleNamespace(sandbox_id="sb-c")],
        snapshot_template_id="tmpl-x",
    )
    await clone_sandbox("sb-src", count=3, timeout=1800)
    assert _FakePPIOSandbox._clone_calls == [("sb-src", 3, 1800)]


# ---------------------------------------------------------------------------
# resume_or_create_sandbox
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_or_create_resumes_paused(monkeypatch):
    fake_sb = _FakeSandbox("sb-paused")

    async def _get_or_start(sid, stype):
        return fake_sb

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)

    sandbox_info = {"id": "sb-paused", "state": "paused"}
    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=sandbox_info)
    assert action == "resumed"
    assert obj is fake_sb
    assert _FakePPIOSandbox._cls_resume_calls


@pytest.mark.asyncio
async def test_resume_or_create_falls_back_on_resume_failure(monkeypatch):
    fake_new = _FakeSandbox("sb-new")

    async def _get_or_start(sid, stype):
        raise RuntimeError("resume failed")

    async def _create(pw, pid, stype):
        return fake_new

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.sandbox.create_sandbox", _create)

    sandbox_info = {"id": "sb-old", "state": "paused"}
    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=sandbox_info)
    assert action == "created"
    assert obj is fake_new


@pytest.mark.asyncio
async def test_resume_or_create_creates_when_not_paused(monkeypatch):
    fake_existing = _FakeSandbox("sb-running")

    async def _get_or_start(sid, stype):
        return fake_existing

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)

    sandbox_info = {"id": "sb-running", "state": "running"}
    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=sandbox_info)
    assert action == "resumed"
    assert obj is fake_existing


@pytest.mark.asyncio
async def test_resume_or_create_running_falls_back_to_create_on_attach_error(monkeypatch):
    fake_new = _FakeSandbox("sb-fresh")

    async def _get_or_start(sid, stype):
        raise RuntimeError("attach failed")

    async def _create(pw, pid, stype):
        return fake_new

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.sandbox.create_sandbox", _create)

    sandbox_info = {"id": "sb-running", "state": "running"}
    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=sandbox_info)
    assert action == "created"
    assert obj is fake_new


@pytest.mark.asyncio
async def test_resume_or_create_creates_when_reattach_probe_fails(monkeypatch):
    fake_dead = _FakeSandbox("sb-running", running=False)
    fake_new = _FakeSandbox("sb-fresh", running=True)

    async def _get_or_start(sid, stype):
        return fake_dead

    async def _create(pw, pid, stype):
        return fake_new

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.sandbox.create_sandbox", _create)

    sandbox_info = {"id": "sb-running", "state": "running"}
    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=sandbox_info)
    assert action == "created"
    assert obj is fake_new


@pytest.mark.asyncio
async def test_resume_or_create_without_create_raises_after_original_exhausted(monkeypatch):
    create_mock = AsyncMock()

    async def _get_or_start(_sid, _stype):
        raise RuntimeError("attach failed")

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.sandbox.create_sandbox", create_mock)

    sandbox_info = {"id": "sb-running", "state": "running"}

    with pytest.raises(SandboxOriginalReuseExhausted) as exc_info:
        await resume_or_create_sandbox(
            "pw",
            "proj-1",
            sandbox_info=sandbox_info,
            allow_create=False,
        )

    assert exc_info.value.sandbox_id == "sb-running"
    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_attempt_ppio_resume_timeout_returns_false(monkeypatch):
    async def _slow_to_thread(*_args, **_kwargs):
        await asyncio.sleep(0.05)
        return True

    monkeypatch.setattr("sandbox.sandbox.asyncio.to_thread", _slow_to_thread)

    result = await sandbox_module._attempt_ppio_resume(
        "sb-timeout",
        timeout_seconds=0.01,
    )

    assert result == (False, None)


@pytest.mark.asyncio
async def test_get_or_start_sandbox_connect_passes_timeout(monkeypatch):
    calls = []

    class _FakeSDKSandbox:
        @staticmethod
        def connect(*args, **kwargs):
            calls.append((args, kwargs))
            return _FakeSandbox("sb-timeout")

    monkeypatch.setitem(sys.modules, "e2b_code_interpreter", SimpleNamespace(Sandbox=_FakeSDKSandbox))
    monkeypatch.setattr(
        "sandbox.sandbox.config",
        SimpleNamespace(SANDBOX_CONNECT_TIMEOUT_ON_RESUME_SECONDS=1234),
    )

    sandbox = await get_or_start_sandbox("sb-timeout", "code")

    assert sandbox.sandbox_id == "sb-timeout"
    assert calls, "Sandbox.connect should be attempted"
    assert any(call_kwargs.get("timeout") == 1234 for _call_args, call_kwargs in calls)


@pytest.mark.asyncio
async def test_resume_or_create_creates_when_no_id(monkeypatch):
    fake_new = _FakeSandbox("sb-brand-new")

    async def _create(pw, pid, stype):
        return fake_new

    monkeypatch.setattr("sandbox.sandbox.create_sandbox", _create)

    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info={})
    assert action == "created"


@pytest.mark.asyncio
async def test_resume_or_create_creates_when_none(monkeypatch):
    fake_new = _FakeSandbox("sb-none")

    async def _create(pw, pid, stype):
        return fake_new

    monkeypatch.setattr("sandbox.sandbox.create_sandbox", _create)

    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=None)
    assert action == "created"


@pytest.mark.asyncio
async def test_create_sandbox_retries_retryable_provider_failure_with_pacing(monkeypatch):
    create_attempts = {"count": 0}
    pacing_calls: list[tuple[str, str, float | None]] = []
    sleep_calls: list[float] = []

    class _FakeCodeSandbox:
        def __init__(self, *args, **kwargs):
            create_attempts["count"] += 1
            if create_attempts["count"] < 3:
                raise SandboxProviderFailure(
                    operation="create",
                    detail="rate limited",
                    root_cause_code="SANDBOX_PROVIDER_RATE_LIMITED",
                    retryable=True,
                    sandbox_type="code",
                    provider_reason="RATE_LIMITED",
                    provider_http_status=429,
                )
            self.sandbox_id = "sb-created"

    async def _wait_for_start_slot(*, project_id: str, sandbox_type: str, queue_timeout_seconds=None, interval_seconds=None):
        pacing_calls.append((project_id, sandbox_type, interval_seconds))
        return 0.0

    async def _fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        return None

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=_FakeCodeSandbox),
    )
    monkeypatch.setattr(sandbox_module, "setup_environment_variables", AsyncMock())
    monkeypatch.setattr(sandbox_module, "start_supervisord_session", AsyncMock())
    monkeypatch.setattr(
        sandbox_module.sandbox_create_capacity,
        "wait_for_sandbox_create_start_slot",
        _wait_for_start_slot,
    )
    monkeypatch.setattr(sandbox_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setenv("SANDBOX_CREATE_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS", "0.5")
    monkeypatch.setenv("SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS", "2.0")

    sandbox = await create_sandbox("pw", "proj-1", "code")

    assert sandbox.sandbox_id == "sb-created"
    assert create_attempts["count"] == 3
    assert len(pacing_calls) == 3
    assert sleep_calls == [0.5, 1.0]


@pytest.mark.asyncio
async def test_create_sandbox_retries_plain_http_429_with_pacing(monkeypatch):
    create_attempts = {"count": 0}
    pacing_calls: list[tuple[str, str, float | None]] = []
    sleep_calls: list[float] = []

    class _FakeCodeSandbox:
        def __init__(self, *args, **kwargs):
            create_attempts["count"] += 1
            if create_attempts["count"] < 3:
                raise RuntimeError("429: Rate limit exceeded, please try again later.")
            self.sandbox_id = "sb-created"

    async def _wait_for_start_slot(*, project_id: str, sandbox_type: str, queue_timeout_seconds=None, interval_seconds=None):
        pacing_calls.append((project_id, sandbox_type, interval_seconds))
        return 0.0

    async def _fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        return None

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=_FakeCodeSandbox),
    )
    monkeypatch.setattr(sandbox_module, "setup_environment_variables", AsyncMock())
    monkeypatch.setattr(sandbox_module, "start_supervisord_session", AsyncMock())
    monkeypatch.setattr(
        sandbox_module.sandbox_create_capacity,
        "wait_for_sandbox_create_start_slot",
        _wait_for_start_slot,
    )
    monkeypatch.setattr(sandbox_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setenv("SANDBOX_CREATE_RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS", "0.5")
    monkeypatch.setenv("SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS", "2.0")

    sandbox = await create_sandbox("pw", "proj-1", "code")

    assert sandbox.sandbox_id == "sb-created"
    assert create_attempts["count"] == 3
    assert len(pacing_calls) == 3
    assert sleep_calls == [0.5, 1.0]


@pytest.mark.asyncio
async def test_create_sandbox_fast_fails_nonretryable_provider_failure(monkeypatch):
    create_attempts = {"count": 0}
    pacing_calls: list[tuple[str, str]] = []

    class _FakeCodeSandbox:
        def __init__(self, *args, **kwargs):
            create_attempts["count"] += 1
            raise SandboxProviderFailure(
                operation="create",
                detail="balance not enough",
                root_cause_code="SANDBOX_PROVIDER_BALANCE_NOT_ENOUGH",
                retryable=False,
                sandbox_type="code",
                provider_reason="BALANCE_NOT_ENOUGH",
            )

    async def _wait_for_start_slot(*, project_id: str, sandbox_type: str, **_kwargs):
        pacing_calls.append((project_id, sandbox_type))
        return 0.0

    monkeypatch.setitem(
        sys.modules,
        "e2b_code_interpreter",
        SimpleNamespace(Sandbox=_FakeCodeSandbox),
    )
    monkeypatch.setattr(sandbox_module, "setup_environment_variables", AsyncMock())
    monkeypatch.setattr(sandbox_module, "start_supervisord_session", AsyncMock())
    monkeypatch.setattr(
        sandbox_module.sandbox_create_capacity,
        "wait_for_sandbox_create_start_slot",
        _wait_for_start_slot,
    )
    monkeypatch.setenv("SANDBOX_CREATE_RETRY_MAX_ATTEMPTS", "5")

    with pytest.raises(SandboxProviderFailure, match="balance not enough"):
        await create_sandbox("pw", "proj-1", "code")

    assert create_attempts["count"] == 1
    assert pacing_calls == [("proj-1", "code")]
