import importlib.util
from pathlib import Path

CONSTANTS_PATH = (
    Path(__file__).resolve().parents[1]
    / "agentscope_integration"
    / "shadow_clone"
    / "constants.py"
)


def _reload_constants():
    module_spec = importlib.util.spec_from_file_location(
        "shadow_clone_constants_for_test",
        CONSTANTS_PATH,
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_resolve_shadow_clone_mode_prefers_explicit_mode(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_DEFAULT_MODE", "auto")
    constants = _reload_constants()

    resolved = constants.resolve_shadow_clone_mode(
        shadow_clone_mode="on",
        shadow_clone_enabled=False,
        execution_origin="interactive",
    )

    assert resolved == constants.ShadowCloneMode.ON


def test_resolve_shadow_clone_mode_accepts_explicit_enum_instance(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_DEFAULT_MODE", "auto")
    constants = _reload_constants()

    resolved = constants.resolve_shadow_clone_mode(
        shadow_clone_mode=constants.ShadowCloneMode.V2,
        shadow_clone_enabled=None,
        execution_origin="interactive",
    )

    assert resolved == constants.ShadowCloneMode.V2


def test_resolve_shadow_clone_mode_supports_legacy_bool(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_DEFAULT_MODE", "auto")
    constants = _reload_constants()

    assert constants.resolve_shadow_clone_mode(None, True, "interactive") == constants.ShadowCloneMode.ON
    assert constants.resolve_shadow_clone_mode(None, False, "interactive") == constants.ShadowCloneMode.OFF


def test_resolve_shadow_clone_mode_invalid_falls_back_to_auto(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_DEFAULT_MODE", "auto")
    constants = _reload_constants()

    resolved = constants.resolve_shadow_clone_mode(
        shadow_clone_mode="invalid-mode",
        shadow_clone_enabled=None,
        execution_origin="interactive",
    )

    assert resolved == constants.ShadowCloneMode.AUTO


def test_resolve_shadow_clone_mode_automation_forces_off(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_DEFAULT_MODE", "auto")
    constants = _reload_constants()

    for origin in ("automation", "trigger", "workflow"):
        resolved = constants.resolve_shadow_clone_mode(
            shadow_clone_mode="on",
            shadow_clone_enabled=True,
            execution_origin=origin,
        )
        assert resolved == constants.ShadowCloneMode.OFF


def test_default_mode_invalid_env_falls_back_to_v2(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_DEFAULT_MODE", "broken")
    constants = _reload_constants()

    assert constants.SHADOW_CLONE_DEFAULT_MODE == constants.ShadowCloneMode.V2


def test_shadow_clone_redis_write_settings_fallback_to_agentscope_env(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_REDIS_WRITE_TIMEOUT_SECONDS", "12.5")
    monkeypatch.setenv("AGENTSCOPE_REDIS_WRITE_RETRY_ATTEMPTS", "4")
    constants = _reload_constants()

    assert constants.SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS == 12.5
    assert constants.SHADOW_CLONE_REDIS_WRITE_RETRY_ATTEMPTS == 4


def test_shadow_clone_confirmation_timeout_defaults_to_none(monkeypatch):
    monkeypatch.delenv("SHADOW_CLONE_CONFIRMATION_TIMEOUT", raising=False)
    constants = _reload_constants()

    assert constants.SHADOW_CLONE_CONFIRMATION_TIMEOUT is None


def test_shadow_clone_confirmation_timeout_supports_explicit_numeric_env(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_CONFIRMATION_TIMEOUT", "120")
    constants = _reload_constants()

    assert constants.SHADOW_CLONE_CONFIRMATION_TIMEOUT == 120


def test_shadow_clone_batch_settings_default_to_bounded_values(monkeypatch):
    monkeypatch.delenv("SHADOW_CLONE_MAX_CONCURRENT", raising=False)
    monkeypatch.delenv("SHADOW_CLONE_PLAN_BATCH_TARGET", raising=False)
    monkeypatch.delenv("SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW", raising=False)
    constants = _reload_constants()

    assert constants.SHADOW_CLONE_MAX_CONCURRENT == 10
    assert constants.SHADOW_CLONE_PLAN_BATCH_TARGET == 10
    assert constants.SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW == 10


def test_shadow_clone_runtime_dispatch_window_defaults_to_max_concurrent(
    monkeypatch,
):
    monkeypatch.setenv("SHADOW_CLONE_MAX_CONCURRENT", "8")
    monkeypatch.setenv("SHADOW_CLONE_PLAN_BATCH_TARGET", "3")
    monkeypatch.delenv("SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW", raising=False)
    constants = _reload_constants()

    assert constants.SHADOW_CLONE_PLAN_BATCH_TARGET == 3
    assert constants.SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW == 8


def test_shadow_clone_runtime_dispatch_window_is_clamped_to_max_concurrent(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_MAX_CONCURRENT", "6")
    monkeypatch.setenv("SHADOW_CLONE_PLAN_BATCH_TARGET", "9")
    monkeypatch.setenv("SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW", "12")
    constants = _reload_constants()

    assert constants.SHADOW_CLONE_PLAN_BATCH_TARGET == 6
    assert constants.SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW == 6
