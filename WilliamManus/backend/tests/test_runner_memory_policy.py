import sys
import types
from pathlib import Path

import pytest

# Minimal stubs for optional infra deps required during module import.
if "structlog" not in sys.modules:
    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    dummy_structlog = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )
    sys.modules["structlog"] = dummy_structlog

if "services.postgresql" not in sys.modules:
    services_pkg = types.ModuleType("services")
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    postgresql_mod.DBConnection = _DummyDBConnection
    services_pkg.postgresql = postgresql_mod
    sys.modules["services"] = services_pkg
    sys.modules["services.postgresql"] = postgresql_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from agentscope_integration.runner import AgentScopeRunner
except Exception as exc:  # pragma: no cover - env-specific optional deps
    pytest.skip(f"Skipping runner policy test due to import error: {exc}", allow_module_level=True)

from agentscope.model import ChatModelBase


def test_glm_uses_openai_policy():
    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="glm-4.7")
    policy = runner._resolve_tool_memory_policy("glm-4.7")
    assert policy.api_format == "openai"
    assert policy.keep_tool_results is True
    assert policy.drop_tool_call_only is False


def test_minimax_and_doubao_use_openai_policy():
    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="minimax-m2.5")

    minimax_policy = runner._resolve_tool_memory_policy("minimax-m2.5")
    minimax_m27_policy = runner._resolve_tool_memory_policy("openrouter-minimax-m2.7")
    mimo_policy = runner._resolve_tool_memory_policy("mimo-v2-pro")
    proxy_gpt_policy = runner._resolve_tool_memory_policy("proxy-gpt-5.4")
    doubao_policy = runner._resolve_tool_memory_policy("doubao-seed-2-0-pro-260215")

    assert minimax_policy.api_format == "openai"
    assert minimax_policy.keep_tool_results is True
    assert minimax_policy.drop_tool_call_only is False

    assert minimax_m27_policy.api_format == "openai"
    assert minimax_m27_policy.keep_tool_results is True
    assert minimax_m27_policy.drop_tool_call_only is False

    assert mimo_policy.api_format == "openai"
    assert mimo_policy.keep_tool_results is True
    assert mimo_policy.drop_tool_call_only is False

    assert proxy_gpt_policy.api_format == "openai"
    assert proxy_gpt_policy.keep_tool_results is True
    assert proxy_gpt_policy.drop_tool_call_only is False

    assert doubao_policy.api_format == "openai"
    assert doubao_policy.keep_tool_results is True
    assert doubao_policy.drop_tool_call_only is False


def test_qwen_uses_openai_policy_and_keeps_recent_tool_history() -> None:
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    policy = runner._resolve_tool_memory_policy("qwen3.5-plus")

    assert policy.api_format == "openai"
    assert policy.keep_tool_results is True
    assert policy.drop_tool_call_only is False
    assert runner._retain_complete_tool_runs == 3


def test_qwen_can_reenable_kv_ready_with_env_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTSCOPE_KV_READY_APPEND_ONLY", "true")
    monkeypatch.setenv("AGENTSCOPE_QWEN_DISABLE_KV_READY", "false")

    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    runner._memory_policy = runner._resolve_tool_memory_policy("qwen3.5-plus")

    assert runner._uses_openai_memory_policy() is True
    assert runner._effective_kv_ready_append_only() is True
    assert runner._effective_token_budgeting() is True


def test_gemini_uses_strict_policy():
    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="gemini-3-flash")
    policy = runner._resolve_tool_memory_policy("gemini-3-flash")
    assert policy.api_format == "gemini_like"
    assert policy.keep_tool_results is True
    assert policy.drop_tool_call_only is True


def test_runner_reads_new_tool_history_env_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTSCOPE_RETAIN_COMPLETE_TOOL_RUNS", "4")
    monkeypatch.setenv("AGENTSCOPE_ENABLE_TOOL_HISTORY_SUMMARY", "false")
    monkeypatch.setenv("AGENTSCOPE_TOOL_HISTORY_SUMMARY_MAX_RUNS", "12")
    monkeypatch.setenv("AGENTSCOPE_TOOL_HISTORY_SUMMARY_MAX_CHARS", "5000")

    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="glm-4.7")

    assert runner._retain_complete_tool_runs == 4
    assert runner._enable_tool_history_summary is False
    assert runner._tool_history_summary_max_runs == 12
    assert runner._tool_history_summary_max_chars == 5000


def test_runner_reads_kv_cache_env_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_ENABLED", "true")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_PROVIDER_MODE", "openrouter")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_BREAKPOINT_MODE", "manual")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_SESSION_STICKY", "false")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_SESSION_TTL_SECONDS", "1800")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_CANONICAL_JSON", "false")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_METRICS_ENABLED", "false")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_SHADOW_LOG_ONLY", "true")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_MIN_PREFIX_TOKENS", "3072")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_CONTRACT_VERSION", "v1")
    monkeypatch.setenv("AGENTSCOPE_KV_READY_APPEND_ONLY", "true")
    monkeypatch.setenv("AGENTSCOPE_QWEN_DISABLE_KV_READY", "false")

    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="glm-4.7")
    runner._memory_policy = runner._resolve_tool_memory_policy("glm-4.7")
    options = runner._kv_cache_options()

    assert runner._kv_cache_enabled is True
    assert runner._kv_cache_breakpoint_mode == "manual"
    assert runner._kv_cache_session_sticky is False
    assert runner._kv_cache_session_ttl_seconds == 1800
    assert runner._kv_cache_canonical_json is False
    assert runner._kv_cache_metrics_enabled is False
    assert runner._kv_cache_shadow_log_only is True
    assert runner._kv_cache_min_prefix_tokens == 3072
    assert runner._kv_cache_contract_version == "v1"
    assert options["enabled"] is True
    assert options["breakpoint_mode"] == "manual"


def test_runner_normalizes_image_media_refs() -> None:
    refs = AgentScopeRunner._normalize_image_media_refs(
        [
            {
                "kind": "image",
                "path": "/workspace/a.png",
                "mime_type": "image/png",
                "filename": "a.png",
                "sha256": "abc",
            },
            {
                "kind": "video",
                "path": "/workspace/a.mp4",
                "mime_type": "video/mp4",
                "filename": "a.mp4",
            },
            {
                "kind": "image",
                "path": "/tmp/not_allowed.png",
                "mime_type": "image/png",
                "filename": "not_allowed.png",
            },
        ]
    )

    assert refs == [
        {
            "kind": "image",
            "path": "/workspace/a.png",
            "mime_type": "image/png",
            "filename": "a.png",
            "sha256": "abc",
        },
    ]


def test_runner_kv_cache_options_disabled_when_kv_ready_inactive() -> None:
    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="gemini-3-flash")
    runner._memory_policy = runner._resolve_tool_memory_policy("gemini-3-flash")
    options = runner._kv_cache_options()
    assert options["enabled"] is False


def test_runner_supports_legacy_retain_recent_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTSCOPE_RETAIN_COMPLETE_TOOL_RUNS", raising=False)
    monkeypatch.setenv("AGENTSCOPE_RETAIN_RECENT_TOOL_RUNS", "5")

    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="glm-4.7")
    assert runner._retain_complete_tool_runs == 5


class _NoopModel(ChatModelBase):
    async def __call__(self, *_args, **_kwargs):
        return {"ok": True}


def test_runner_global_fallback_defaults_to_glm47() -> None:
    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="gemini-3-flash")
    assert runner._global_fallback_model_key == "glm-4.7"


def test_runner_wraps_model_with_global_fallback() -> None:
    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="gemini-3-flash")
    wrapped = runner._wrap_with_global_fallback(
        _NoopModel("google/gemini-3-flash-preview", stream=False),
        "gemini-3-flash",
    )
    assert wrapped.__class__.__name__ == "GlobalFallbackChatModel"


def test_runner_qwen_uses_retry_without_cross_model_fallback_by_default() -> None:
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    wrapped = runner._wrap_with_global_fallback(
        _NoopModel("qwen3.5-plus", stream=True),
        "qwen3.5-plus",
    )

    assert wrapped.__class__.__name__ == "GlobalFallbackChatModel"
    assert wrapped._primary_max_retries == 2
    assert wrapped._fallback_model_key == ""
    assert wrapped._fallback_on_stream_error_after_output is False


def test_runner_qwen_can_enable_last_alternative_when_cross_fallback_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENTSCOPE_QWEN_DISABLE_CROSS_MODEL_FALLBACK", "false")
    monkeypatch.setenv("AGENTSCOPE_QWEN_PRIMARY_RETRIES", "1")

    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    wrapped = runner._wrap_with_global_fallback(
        _NoopModel("qwen3.5-plus", stream=True),
        "qwen3.5-plus",
    )

    assert wrapped.__class__.__name__ == "GlobalFallbackChatModel"
    assert wrapped._primary_max_retries == 1
    assert wrapped._fallback_model_key == "glm-4.7"


def test_runner_qwen_can_disable_last_alternative(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTSCOPE_QWEN_PRIMARY_RETRIES", "1")
    monkeypatch.setenv("AGENTSCOPE_QWEN_DISABLE_CROSS_MODEL_FALLBACK", "false")
    monkeypatch.setenv("AGENTSCOPE_QWEN_LAST_ALTERNATIVE_ENABLED", "false")

    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    wrapped = runner._wrap_with_global_fallback(
        _NoopModel("qwen3.5-plus", stream=True),
        "qwen3.5-plus",
    )

    assert wrapped.__class__.__name__ == "GlobalFallbackChatModel"
    assert wrapped._primary_max_retries == 1
    assert wrapped._fallback_model_key == ""


def test_runner_qwen_image_dual_stage_enabled_by_default() -> None:
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    assert runner._qwen_image_dual_stage_enabled is True
    assert runner._qwen_second_visual_pass_enabled is True


def test_runner_qwen_image_dual_stage_respects_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTSCOPE_QWEN_IMAGE_DUAL_STAGE_ENABLED", "false")
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    assert runner._qwen_image_dual_stage_enabled is False


def test_runner_should_use_qwen_image_dual_stage() -> None:
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    refs = [
        {
            "kind": "image",
            "path": "/workspace/a.png",
            "mime_type": "image/png",
            "filename": "a.png",
        },
    ]
    assert runner._should_use_qwen_image_dual_stage(
        use_native_multimodal=True,
        image_media_refs=refs,
    ) is True
    assert runner._should_use_qwen_image_dual_stage(
        use_native_multimodal=False,
        image_media_refs=refs,
    ) is False


def test_runner_build_grounded_user_message_includes_sections() -> None:
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    grounded = runner._build_grounded_user_message(
        original_user_message="Please recreate this page",
        grounding_summary="The page has a blue header and a two-column card grid.",
    )
    assert "<vision-grounding>" in grounded
    assert "blue header" in grounded
    assert "## Current User Request" in grounded


def test_runner_should_run_second_visual_pass_for_high_fidelity_with_uncertainty() -> None:
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    payload = {
        "summary": "Some visual details are uncertain.",
        "structured": {"uncertainties": ["Button radius is unclear", "Spacing may be 20px"]},
    }
    assert runner._should_run_qwen_second_visual_pass(
        user_message="Please replicate this webpage pixel-perfect.",
        grounding_payload=payload,
    ) is True


def test_runner_skips_second_visual_pass_when_not_high_fidelity() -> None:
    runner = AgentScopeRunner(
        thread_id="t1",
        project_id="p1",
        model_key="qwen3.5-plus",
    )
    payload = {
        "summary": "Some visual details are uncertain.",
        "structured": {"uncertainties": ["Card height unclear"]},
    }
    assert runner._should_run_qwen_second_visual_pass(
        user_message="Summarize what this image is about.",
        grounding_payload=payload,
    ) is False
