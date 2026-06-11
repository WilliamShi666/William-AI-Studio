from agentscope_integration.context_builder import ContextBuilder


def test_context_builder_builds_orchestrator_memory_profile(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_ENABLE_TOOL_HISTORY_SUMMARY", "false")
    monkeypatch.setenv("AGENTSCOPE_KV_READY_APPEND_ONLY", "true")
    monkeypatch.setenv("AGENTSCOPE_QWEN_DISABLE_KV_READY", "true")

    builder = ContextBuilder.from_env()
    kwargs = builder.build_orchestrator_memory_kwargs(
        thread_id="thread-1",
        project_id="project-1",
        model_key="kimi-k2.5",
    )

    assert kwargs["thread_id"] == "thread-1"
    assert kwargs["project_id"] == "project-1"
    assert kwargs["exclude_tool_calls"] is True
    assert kwargs["keep_tool_results"] is True
    assert kwargs["drop_tool_call_only"] is False
    assert kwargs["enable_tool_history_summary"] is False


def test_context_builder_builds_shadow_clone_tail_memory_profile(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_ENABLE_TOOL_HISTORY_SUMMARY", "true")
    monkeypatch.setenv("AGENTSCOPE_KV_READY_APPEND_ONLY", "true")

    builder = ContextBuilder.from_env()
    kwargs = builder.build_shadow_clone_tail_memory_kwargs(
        thread_id="thread-1",
        project_id="project-1",
        thread_run_id="run-1",
        model_key="kimi-k2.5",
    )

    assert kwargs["thread_id"] == "thread-1"
    assert kwargs["project_id"] == "project-1"
    assert kwargs["thread_run_id"] == "run-1"
    assert kwargs["exclude_tool_calls"] is True


def test_context_builder_builds_worker_memory_profile(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_CANONICAL_JSON", "false")
    monkeypatch.setenv("AGENTSCOPE_KV_CACHE_CONTRACT_VERSION", "v1")

    builder = ContextBuilder.from_env()
    kwargs = builder.build_worker_memory_kwargs(
        thread_id="thread-1",
        project_id="project-1",
        thread_run_id="run-1",
        model_key="gemini-3-flash",
    )

    assert kwargs["thread_run_id"] == "run-1"
    assert kwargs["exclude_tool_calls"] is False
    assert kwargs["drop_tool_call_only"] is True
    assert kwargs["enable_tool_history_summary"] is True
    assert kwargs["kv_cache_canonical_json"] is False


def test_context_builder_deepseek_keeps_tool_call_history_by_default(monkeypatch):
    """DeepSeek quality mode needs complete tool-call/result pairs in memory."""
    monkeypatch.delenv("AGENTSCOPE_EXCLUDE_TOOL_CALLS", raising=False)

    builder = ContextBuilder.from_env()
    kwargs = builder.build_orchestrator_memory_kwargs(
        thread_id="thread-1",
        project_id="project-1",
        model_key="deepseek-v4-pro-max",
    )

    assert kwargs["exclude_tool_calls"] is False
    assert kwargs["keep_tool_results"] is True
    assert kwargs["drop_tool_call_only"] is False


def test_context_builder_can_opt_back_to_legacy_tool_call_exclusion(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_EXCLUDE_TOOL_CALLS", "true")

    builder = ContextBuilder.from_env()
    kwargs = builder.build_orchestrator_memory_kwargs(
        thread_id="thread-1",
        project_id="project-1",
        model_key="deepseek-v4-pro-max",
    )

    assert kwargs["exclude_tool_calls"] is True
