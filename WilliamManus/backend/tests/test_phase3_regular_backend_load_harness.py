from __future__ import annotations

import asyncio
import importlib.util
import importlib
from pathlib import Path
import sys

import pytest

HARNESS_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "phase3_regular_backend_load_harness.py"
)


def _load_harness_module():
    for module_name in list(sys.modules):
        if (
            module_name == "phase3_regular_backend_load_harness"
            or module_name == "agent"
            or module_name == "services"
            or module_name == "utils"
            or module_name.startswith("agent.")
            or module_name.startswith("services.")
            or module_name.startswith("utils.")
        ):
            sys.modules.pop(module_name, None)

    spec = importlib.util.spec_from_file_location(
        "phase3_regular_backend_load_harness",
        HARNESS_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bootstrap_worktree_runtime_environment_loads_runtime_meta_defaults(
    tmp_path,
    monkeypatch,
):
    meta_path = tmp_path / "backend.meta"
    meta_path.write_text(
        "\n".join(
            [
                "worktree_root=/path/to/williams-ai-studio",
                "runtime_token=runtime-123",
                "redis_db=15",
                "dramatiq_run_agent_queue=run_agent_background_worktree_8003_runtime-123",
                "dramatiq_sandbox_cleanup_queue=sandbox_cleanup_worktree_8003_runtime-123",
                "shadow_clone_subagent_queue=shadow_clone_subagents_worktree_8003_runtime-123",
                "regular_supervisor_queue=regular_supervisor_worktree_8003_runtime-123",
                "agentscope_server_concurrency_budget=128",
                "agentscope_server_regular_admission_budget=128",
                "regular_queue_max_depth=256",
                "regular_supervisor_queue_max_depth=256",
                "sandbox_shared_max_concurrent_global=128",
                "sandbox_shared_max_concurrent_per_sandbox=20",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "REGULAR_LOAD_HARNESS_RUNTIME_META_PATH",
        str(meta_path),
    )
    monkeypatch.delenv("WORKTREE_RUNTIME_TOKEN", raising=False)
    monkeypatch.delenv("REDIS_DB", raising=False)
    monkeypatch.delenv("DRAMATIQ_RUN_AGENT_QUEUE", raising=False)
    monkeypatch.delenv("DRAMATIQ_SANDBOX_CLEANUP_QUEUE", raising=False)
    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_QUEUE", raising=False)
    monkeypatch.delenv("REGULAR_SUPERVISOR_QUEUE", raising=False)
    monkeypatch.delenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", raising=False)
    monkeypatch.delenv("AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET", raising=False)
    monkeypatch.delenv("REGULAR_QUEUE_MAX_DEPTH", raising=False)
    monkeypatch.delenv("REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH", raising=False)
    monkeypatch.delenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", raising=False)
    monkeypatch.delenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", raising=False)

    module = _load_harness_module()
    queue_names = importlib.import_module("utils.dramatiq_queue_names")

    assert module._BOOTSTRAPPED_RUNTIME_ENV == {
        "WORKTREE_RUNTIME_TOKEN": "runtime-123",
        "REDIS_DB": "15",
        "DRAMATIQ_RUN_AGENT_QUEUE": "run_agent_background_worktree_8003_runtime-123",
        "DRAMATIQ_SANDBOX_CLEANUP_QUEUE": "sandbox_cleanup_worktree_8003_runtime-123",
        "SHADOW_CLONE_SUBAGENT_QUEUE": "shadow_clone_subagents_worktree_8003_runtime-123",
        "REGULAR_SUPERVISOR_QUEUE": "regular_supervisor_worktree_8003_runtime-123",
        "AGENTSCOPE_SERVER_CONCURRENCY_BUDGET": "128",
        "AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET": "128",
        "REGULAR_QUEUE_MAX_DEPTH": "256",
        "REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH": "256",
        "SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL": "128",
        "SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX": "20",
    }
    assert module.regular_supervisor_metrics.get_runtime_token() == "runtime-123"
    assert module.redis_service._load_redis_env()[3] == 15
    assert (
        queue_names.RUN_AGENT_BACKGROUND_QUEUE
        == "run_agent_background_worktree_8003_runtime-123"
    )


def test_validate_worktree_runtime_binding_raises_on_mismatched_runtime_meta(
    tmp_path,
    monkeypatch,
):
    meta_path = tmp_path / "backend.meta"
    meta_path.write_text(
        "\n".join(
            [
                "worktree_root=/path/to/williams-ai-studio",
                "runtime_token=runtime-123",
                "redis_db=15",
                "dramatiq_run_agent_queue=run_agent_background_worktree_8003_runtime-123",
                "dramatiq_sandbox_cleanup_queue=sandbox_cleanup_worktree_8003_runtime-123",
                "shadow_clone_subagent_queue=shadow_clone_subagents_worktree_8003_runtime-123",
                "regular_supervisor_queue=regular_supervisor_worktree_8003_runtime-123",
                "regular_queue_max_depth=256",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "REGULAR_LOAD_HARNESS_RUNTIME_META_PATH",
        str(meta_path),
    )
    monkeypatch.setenv("WORKTREE_RUNTIME_TOKEN", "wrong-runtime")
    monkeypatch.setenv("REDIS_DB", "0")
    monkeypatch.setenv("DRAMATIQ_RUN_AGENT_QUEUE", "default")
    monkeypatch.setenv("DRAMATIQ_SANDBOX_CLEANUP_QUEUE", "sandbox_cleanup")
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_QUEUE", "shadow_clone_subagents")
    monkeypatch.setenv("REGULAR_SUPERVISOR_QUEUE", "regular_supervisor")
    monkeypatch.setenv("REGULAR_QUEUE_MAX_DEPTH", "32")

    module = _load_harness_module()

    with pytest.raises(RuntimeError, match="WORKTREE_RUNTIME_TOKEN|REGULAR_QUEUE_MAX_DEPTH"):
        module.validate_worktree_runtime_binding()


def test_argument_parser_leaves_validation_profile_overrides_unset_by_default():
    module = _load_harness_module()

    args = module._build_argument_parser().parse_args([])

    assert args.artifact_run_count is None
    assert args.queued_stop_count is None
    assert args.running_stop_count is None
    assert args.disturb_one_shard is None


def test_harness_initializes_agent_api_with_shared_db_connection():
    module = _load_harness_module()

    assert module.agent_api.db is module.db


@pytest.mark.asyncio
async def test_run_matrix_uses_default_targets(monkeypatch):
    module = _load_harness_module()
    seen_targets: list[int] = []

    async def _fake_run_tier(*, target_active_runs: int, **_kwargs):
        seen_targets.append(target_active_runs)
        return {"target_active_runs": target_active_runs}

    monkeypatch.setattr(module, "run_tier", _fake_run_tier)

    results = await module.run_matrix()

    assert seen_targets == [25, 50, 100]
    assert [result["target_active_runs"] for result in results] == [25, 50, 100]


def test_build_harness_metadata_sets_double_gate_payload():
    module = _load_harness_module()

    metadata = module.build_harness_metadata(
        batch_id="batch-1",
        target_tier=50,
        active_duration_seconds=20.0,
        emit_stub_response=True,
        artifact_path="/workspace/load-harness/result.txt",
    )

    assert metadata["regular_execution_mode"] == "phase2_supervisor"
    assert metadata["regular_load_harness"]["enabled"] is True
    assert metadata["regular_load_harness"]["batch_id"] == "batch-1"
    assert metadata["regular_load_harness"]["target_tier"] == 50
    assert (
        metadata["regular_load_harness"]["artifact_path"]
        == "/workspace/load-harness/result.txt"
    )


def test_build_harness_metadata_defaults_to_stub_backend_workload_profile():
    module = _load_harness_module()

    metadata = module.build_harness_metadata(
        batch_id="batch-1",
        target_tier=16,
        active_duration_seconds=20.0,
        emit_stub_response=True,
        artifact_path="/workspace/load-harness/result.txt",
    )

    assert metadata["regular_load_harness"]["execution_profile"] == "stub_backend"
    assert metadata["regular_load_harness"]["validation_profile"] == "stop_sampled"
    assert (
        metadata["regular_load_harness"]["sandbox_distribution_profile"]
        == "shared_project_single_sandbox"
    )
    assert metadata["regular_load_harness"]["user_message_override"] is None


def test_build_harness_metadata_defaults_model_to_runtime_config(monkeypatch):
    module = _load_harness_module()
    monkeypatch.setattr(module.config, "MODEL_TO_USE", "openrouter/minimax/minimax-m2.5")

    metadata = module.build_harness_metadata(
        batch_id="batch-1",
        target_tier=16,
        active_duration_seconds=20.0,
        emit_stub_response=True,
    )

    assert metadata["model_name"] == "openrouter/minimax/minimax-m2.5"


def test_resolve_harness_model_name_migrates_legacy_qwen_default(monkeypatch):
    module = _load_harness_module()
    monkeypatch.delenv("REGULAR_LOAD_HARNESS_MODEL_NAME", raising=False)
    monkeypatch.setattr(module.config, "MODEL_TO_USE", "dashscope/qwen3.5-397b-a17b")

    assert module._resolve_harness_model_name() == "openrouter/minimax/minimax-m2.5"


def test_resolve_harness_model_name_uses_minimax_when_config_is_empty(monkeypatch):
    module = _load_harness_module()
    monkeypatch.delenv("REGULAR_LOAD_HARNESS_MODEL_NAME", raising=False)
    monkeypatch.setattr(module.config, "MODEL_TO_USE", "")

    assert module._resolve_harness_model_name() == "openrouter/minimax/minimax-m2.5"


def test_build_harness_metadata_sets_real_sandbox_profile_inputs():
    module = _load_harness_module()

    metadata = module.build_harness_metadata(
        batch_id="batch-1",
        target_tier=16,
        active_duration_seconds=20.0,
        emit_stub_response=False,
        artifact_path="/workspace/load-harness/result.txt",
        execution_profile="real_sandbox_write_once",
        validation_profile="baseline_no_stop",
        sandbox_distribution_profile="per_run_project_multi_sandbox",
        user_message_override=module._default_user_message_override(
            execution_profile="real_sandbox_write_once",
            artifact_path="/workspace/load-harness/result.txt",
        ),
    )

    assert (
        metadata["regular_load_harness"]["execution_profile"]
        == "real_sandbox_write_once"
    )
    assert metadata["regular_load_harness"]["validation_profile"] == "baseline_no_stop"
    assert (
        metadata["regular_load_harness"]["sandbox_distribution_profile"]
        == "per_run_project_multi_sandbox"
    )
    assert metadata["regular_load_harness"]["user_message_override"].startswith(
        "You must do exactly these steps:"
    )


def test_build_artifact_path_uses_short_stable_workspace_path():
    module = _load_harness_module()

    assert (
        module.build_artifact_path(batch_id="phase3-task5b-16-batch-1", run_index=0)
        == "/workspace/load-harness/artifact-1.txt"
    )
    assert (
        module.build_artifact_path(batch_id="phase3-task5b-16-batch-1", run_index=15)
        == "/workspace/load-harness/artifact-16.txt"
    )


def test_default_user_message_override_for_real_sandbox_uses_exact_path_contract():
    module = _load_harness_module()

    message = module._default_user_message_override(
        execution_profile="real_sandbox_write_once",
        artifact_path="/workspace/load-harness/artifact-3.txt",
    )

    assert "`/workspace/load-harness/artifact-3.txt`" in str(message)
    assert "Do not change the path" in str(message)
    assert "Reply with exactly `DONE`" in str(message)


def test_resolve_validation_profile_baseline_no_stop_disables_stop_and_disturbance():
    module = _load_harness_module()

    profile = module.resolve_validation_profile("baseline_no_stop")

    assert profile == {
        "validation_profile": "baseline_no_stop",
        "queued_stop_count": 0,
        "running_stop_count": 0,
        "disturb_one_shard": False,
    }


def test_resolve_validation_profile_disturbance_recovery_enables_disturbance():
    module = _load_harness_module()

    profile = module.resolve_validation_profile("disturbance_recovery")

    assert profile == {
        "validation_profile": "disturbance_recovery",
        "queued_stop_count": 2,
        "running_stop_count": 3,
        "disturb_one_shard": True,
    }


def test_resolve_validation_profile_rejects_unknown_profile():
    module = _load_harness_module()

    with pytest.raises(ValueError, match="Unknown validation_profile"):
        module.resolve_validation_profile("definitely_not_a_profile")


def test_resolve_sandbox_distribution_profile_rejects_unknown_value():
    module = _load_harness_module()

    with pytest.raises(ValueError, match="sandbox_distribution_profile"):
        module._resolve_sandbox_distribution_profile("totally_unknown_mode")


def test_resolve_effective_artifact_run_count_defaults_to_zero_for_real_provider_text():
    module = _load_harness_module()

    assert (
        module._resolve_effective_artifact_run_count(
            target_active_runs=25,
            artifact_run_count=None,
            execution_profile="real_provider_text",
        )
        == 0
    )


@pytest.mark.asyncio
async def test_resolve_account_id_uses_first_available_account_when_missing():
    module = _load_harness_module()

    class _FakeQuery:
        def select(self, *_args, **_kwargs):
            return self

        def limit(self, _count: int):
            return self

        async def execute(self):
            return type("Result", (), {"data": [{"id": "account-1"}]})()

    class _FakeSchema:
        def table(self, table_name: str):
            assert table_name == "accounts"
            return _FakeQuery()

    class _FakeClient:
        def schema(self, schema_name: str):
            assert schema_name == "basejump"
            return _FakeSchema()

    resolved = await module.resolve_account_id(
        client=_FakeClient(),
        explicit_account_id=None,
    )

    assert resolved == "account-1"


@pytest.mark.asyncio
async def test_resolve_account_id_falls_back_to_projects_when_basejump_accounts_missing():
    module = _load_harness_module()

    class _FailingBasejumpQuery:
        def select(self, *_args, **_kwargs):
            return self

        def limit(self, _count: int):
            return self

        async def execute(self):
            raise RuntimeError(
                'Database query failed: relation "basejump.accounts" does not exist'
            )

    class _ProjectsQuery:
        def select(self, fields: str):
            assert fields == "account_id"
            return self

        def limit(self, _count: int):
            return self

        async def execute(self):
            return type("Result", (), {"data": [{"account_id": "project-account-1"}]})()

    class _FakeSchema:
        def table(self, table_name: str):
            assert table_name == "accounts"
            return _FailingBasejumpQuery()

    class _FakeClient:
        def schema(self, schema_name: str):
            assert schema_name == "basejump"
            return _FakeSchema()

        def table(self, table_name: str):
            assert table_name == "projects"
            return _ProjectsQuery()

    resolved = await module.resolve_account_id(
        client=_FakeClient(),
        explicit_account_id=None,
    )

    assert resolved == "project-account-1"


@pytest.mark.asyncio
async def test_run_tier_returns_required_metric_keys(monkeypatch):
    module = _load_harness_module()

    async def _fake_create_fixtures(
        *,
        batch_id: str,
        account_id: str,
        target_active_runs: int,
        sandbox_distribution_profile: str,
    ):
        assert target_active_runs == 27
        assert sandbox_distribution_profile == "shared_project_single_sandbox"
        return {
            "batch_id": batch_id,
            "account_id": account_id,
            "projects": [
                {
                    "project_id": "project-1",
                    "project_name": "phase3-task5b-batch-1",
                }
            ],
            "thread_ids": ["thread-1"],
        }

    async def _fake_load_supervisor_counter_snapshots():
        return {"sup-1": {"claim_success_count": 1}}

    async def _fake_admit_runs(**_kwargs):
        return [{"agent_run_id": "run-1"}]

    async def _fake_wait_for_target_active(**_kwargs):
        return {
            "time_to_target_active_ms": 1.0,
            "peak_active_slots": 1,
            "p95_run_start_delay_ms": 2.0,
        }

    async def _fake_issue_stop_sample(**_kwargs):
        return {"p95_stop_latency_ms": 3.0}

    async def _fake_wait_for_terminal_convergence(**_kwargs):
        return {
            "successful_completion_ratio": 1.0,
            "p95_terminal_convergence_ms": 4.0,
        }

    async def _fake_verify_artifact_visibility(**_kwargs):
        return {"artifact_visibility_ratio": 1.0}

    async def _fake_load_supervisor_counter_deltas(_baseline):
        return {"sup-1": {"claim_success_count": 3}}

    async def _fake_maybe_disturb_one_supervisor(**_kwargs):
        return {"enabled": False}

    async def _fake_cleanup_batch(**_kwargs):
        return None

    monkeypatch.setattr(module, "resolve_account_id", lambda **_kwargs: "account-1")
    monkeypatch.setattr(module, "_create_fixtures", _fake_create_fixtures)
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_snapshots",
        _fake_load_supervisor_counter_snapshots,
    )
    monkeypatch.setattr(module, "_admit_runs", _fake_admit_runs)
    monkeypatch.setattr(module, "_wait_for_target_active", _fake_wait_for_target_active)
    monkeypatch.setattr(module, "_issue_stop_sample", _fake_issue_stop_sample)
    monkeypatch.setattr(
        module,
        "_wait_for_terminal_convergence",
        _fake_wait_for_terminal_convergence,
    )
    monkeypatch.setattr(
        module,
        "_verify_artifact_visibility",
        _fake_verify_artifact_visibility,
    )
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_deltas",
        _fake_load_supervisor_counter_deltas,
    )
    monkeypatch.setattr(
        module,
        "_maybe_disturb_one_supervisor",
        _fake_maybe_disturb_one_supervisor,
    )
    monkeypatch.setattr(module, "_cleanup_batch", _fake_cleanup_batch)

    result = await module.run_tier(target_active_runs=25)

    assert set(result) >= {
        "target_active_runs",
        "sandbox_distribution_profile",
        "fixture_project_count",
        "expected_sandbox_count",
        "comparison_key",
        "successful_completion_ratio",
        "artifact_visibility_ratio",
        "time_to_target_active_ms",
        "peak_active_slots",
        "p95_run_start_delay_ms",
        "p95_terminal_convergence_ms",
        "p95_stop_latency_ms",
        "shard_counter_deltas",
        "disturbance",
    }


@pytest.mark.asyncio
async def test_run_tier_defaults_real_sandbox_workload_to_artifact_per_run(
    monkeypatch,
):
    module = _load_harness_module()
    observed: dict[str, object] = {}

    async def _fake_create_fixtures(
        *,
        batch_id: str,
        account_id: str,
        target_active_runs: int,
        sandbox_distribution_profile: str,
    ):
        return {
            "batch_id": batch_id,
            "account_id": account_id,
            "projects": [
                {
                    "project_id": f"project-{index}",
                    "project_name": f"project-{index}",
                }
                for index in range(target_active_runs)
            ],
        }

    async def _fake_admit_runs(**kwargs):
        observed["artifact_run_count"] = kwargs["artifact_run_count"]
        return [{"agent_run_id": "run-1"}]

    async def _fake_wait_for_target_active(**_kwargs):
        return {
            "time_to_target_active_ms": 1.0,
            "peak_active_slots": 1,
            "p95_run_start_delay_ms": 1.0,
        }

    async def _fake_issue_stop_sample(**_kwargs):
        return {"p95_stop_latency_ms": None}

    async def _fake_wait_for_terminal_convergence(**_kwargs):
        return {
            "successful_completion_ratio": 1.0,
            "p95_terminal_convergence_ms": 1.0,
        }

    async def _fake_verify_artifact_visibility(**_kwargs):
        return {"artifact_visibility_ratio": 1.0}

    async def _fake_load_supervisor_counter_snapshots():
        return {}

    async def _fake_load_supervisor_counter_deltas(_baseline):
        return {}

    async def _fake_maybe_disturb_one_supervisor(**_kwargs):
        return {"enabled": False}

    async def _fake_cleanup_batch(**_kwargs):
        return None

    monkeypatch.setattr(module, "resolve_account_id", lambda **_kwargs: "account-1")
    monkeypatch.setattr(module, "_create_fixtures", _fake_create_fixtures)
    monkeypatch.setattr(module, "_admit_runs", _fake_admit_runs)
    monkeypatch.setattr(module, "_wait_for_target_active", _fake_wait_for_target_active)
    monkeypatch.setattr(module, "_issue_stop_sample", _fake_issue_stop_sample)
    monkeypatch.setattr(
        module,
        "_wait_for_terminal_convergence",
        _fake_wait_for_terminal_convergence,
    )
    monkeypatch.setattr(
        module,
        "_verify_artifact_visibility",
        _fake_verify_artifact_visibility,
    )
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_snapshots",
        _fake_load_supervisor_counter_snapshots,
    )
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_deltas",
        _fake_load_supervisor_counter_deltas,
    )
    monkeypatch.setattr(
        module,
        "_maybe_disturb_one_supervisor",
        _fake_maybe_disturb_one_supervisor,
    )
    monkeypatch.setattr(module, "_cleanup_batch", _fake_cleanup_batch)

    await module.run_tier(
        target_active_runs=25,
        execution_profile="real_sandbox_write_once",
    )

    assert observed["artifact_run_count"] == 25


@pytest.mark.asyncio
async def test_run_matrix_forwards_workload_validation_and_sandbox_distribution_profiles(
    monkeypatch,
):
    module = _load_harness_module()
    observed: list[tuple[int, str, str, str]] = []

    async def _fake_run_tier(
        *,
        target_active_runs: int,
        execution_profile: str,
        validation_profile: str,
        sandbox_distribution_profile: str,
        **_kwargs,
    ):
        observed.append(
            (
                target_active_runs,
                execution_profile,
                validation_profile,
                sandbox_distribution_profile,
            )
        )
        return {
            "target_active_runs": target_active_runs,
            "execution_profile": execution_profile,
            "validation_profile": validation_profile,
            "sandbox_distribution_profile": sandbox_distribution_profile,
        }

    monkeypatch.setattr(module, "run_tier", _fake_run_tier)

    results = await module.run_matrix(
        targets=(16, 25),
        execution_profile="real_provider_text",
        validation_profile="baseline_no_stop",
        sandbox_distribution_profile="per_run_project_multi_sandbox",
    )

    assert observed == [
        (
            16,
            "real_provider_text",
            "baseline_no_stop",
            "per_run_project_multi_sandbox",
        ),
        (
            25,
            "real_provider_text",
            "baseline_no_stop",
            "per_run_project_multi_sandbox",
        ),
    ]
    assert [result["execution_profile"] for result in results] == [
        "real_provider_text",
        "real_provider_text",
    ]
    assert [result["validation_profile"] for result in results] == [
        "baseline_no_stop",
        "baseline_no_stop",
    ]
    assert [result["sandbox_distribution_profile"] for result in results] == [
        "per_run_project_multi_sandbox",
        "per_run_project_multi_sandbox",
    ]


@pytest.mark.asyncio
async def test_create_fixtures_shared_project_single_sandbox_creates_one_project(
    monkeypatch,
):
    module = _load_harness_module()
    inserted_projects: list[dict[str, str]] = []

    class _InsertQuery:
        async def insert(self, payload: dict[str, str]):
            inserted_projects.append(dict(payload))
            return type("Result", (), {"data": [payload]})()

    class _Schema:
        def table(self, table_name: str):
            assert table_name == "projects"
            return _InsertQuery()

    class _Client:
        def schema(self, schema_name: str):
            assert schema_name == "public"
            return _Schema()

    async def _fake_get_client():
        return _Client()

    monkeypatch.setattr(module, "_get_client", _fake_get_client)

    fixtures = await module._create_fixtures(
        batch_id="batch-1",
        account_id="account-1",
        target_active_runs=4,
        sandbox_distribution_profile="shared_project_single_sandbox",
    )

    assert len(inserted_projects) == 1
    assert len(fixtures["projects"]) == 1
    assert fixtures["projects"][0]["project_id"] == inserted_projects[0]["project_id"]


@pytest.mark.asyncio
async def test_create_fixtures_per_run_project_multi_sandbox_creates_one_project_per_run(
    monkeypatch,
):
    module = _load_harness_module()
    inserted_projects: list[dict[str, str]] = []

    class _InsertQuery:
        async def insert(self, payload: dict[str, str]):
            inserted_projects.append(dict(payload))
            return type("Result", (), {"data": [payload]})()

    class _Schema:
        def table(self, table_name: str):
            assert table_name == "projects"
            return _InsertQuery()

    class _Client:
        def schema(self, schema_name: str):
            assert schema_name == "public"
            return _Schema()

    async def _fake_get_client():
        return _Client()

    monkeypatch.setattr(module, "_get_client", _fake_get_client)

    fixtures = await module._create_fixtures(
        batch_id="batch-1",
        account_id="account-1",
        target_active_runs=3,
        sandbox_distribution_profile="per_run_project_multi_sandbox",
    )

    assert len(inserted_projects) == 3
    assert [project["project_name"] for project in fixtures["projects"]] == [
        "phase3-task5b-batch-1-run-1",
        "phase3-task5b-batch-1-run-2",
        "phase3-task5b-batch-1-run-3",
    ]


@pytest.mark.asyncio
async def test_create_fixtures_rolls_back_created_projects_when_multi_sandbox_creation_fails(
    monkeypatch,
):
    module = _load_harness_module()
    inserted_project_ids: list[str] = []
    deleted_project_ids: list[str] = []

    class _DeleteQuery:
        def __init__(self, project_id: str):
            self.project_id = project_id

        async def delete(self):
            deleted_project_ids.append(self.project_id)
            return type("Result", (), {"data": [{"project_id": self.project_id}]})()

    class _ProjectsTable:
        def __init__(self):
            self._project_id: str | None = None

        async def insert(self, payload: dict[str, str]):
            inserted_project_ids.append(str(payload["project_id"]))
            if len(inserted_project_ids) == 3:
                raise RuntimeError("project insert failed")
            return type("Result", (), {"data": [payload]})()

        def eq(self, field: str, value: str):
            assert field == "project_id"
            return _DeleteQuery(value)

    class _Schema:
        def table(self, table_name: str):
            assert table_name == "projects"
            return _ProjectsTable()

    class _Client:
        def schema(self, schema_name: str):
            assert schema_name == "public"
            return _Schema()

        def table(self, table_name: str):
            assert table_name == "projects"
            return _ProjectsTable()

    async def _fake_get_client():
        return _Client()

    monkeypatch.setattr(module, "_get_client", _fake_get_client)

    with pytest.raises(RuntimeError, match="project insert failed"):
        await module._create_fixtures(
            batch_id="batch-1",
            account_id="account-1",
            target_active_runs=4,
            sandbox_distribution_profile="per_run_project_multi_sandbox",
        )

    assert deleted_project_ids == inserted_project_ids[:2]


@pytest.mark.asyncio
async def test_fetch_attempt_rows_batches_agent_run_ids_into_single_query():
    module = _load_harness_module()
    query_state: dict[str, object] = {}

    class _FakeQuery:
        def select(self, fields: str):
            query_state["select_fields"] = fields
            return self

        def in_(self, field: str, values: list[object]):
            query_state["in_field"] = field
            query_state["in_values"] = list(values)
            return self

        async def execute(self):
            query_state["execute_calls"] = int(query_state.get("execute_calls", 0)) + 1
            return type("Result", (), {"data": [{"agent_run_id": "run-1"}]})()

    class _FakeClient:
        def table(self, table_name: str):
            query_state["table_name"] = table_name
            return _FakeQuery()

    rows = await module._fetch_attempt_rows(
        _FakeClient(),
        agent_run_ids=["run-1", "run-2"],
    )

    assert rows == [{"agent_run_id": "run-1"}]
    assert query_state["table_name"] == "regular_run_attempts"
    assert query_state["select_fields"] == "*"
    assert query_state["in_field"] == "agent_run_id"
    assert query_state["in_values"] == ["run-1", "run-2"]
    assert query_state["execute_calls"] == 1


@pytest.mark.asyncio
async def test_fetch_run_rows_batches_agent_run_ids_into_single_query():
    module = _load_harness_module()
    query_state: dict[str, object] = {}

    class _FakeQuery:
        def select(self, fields: str):
            query_state["select_fields"] = fields
            return self

        def in_(self, field: str, values: list[object]):
            query_state["in_field"] = field
            query_state["in_values"] = list(values)
            return self

        async def execute(self):
            query_state["execute_calls"] = int(query_state.get("execute_calls", 0)) + 1
            return type(
                "Result", (), {"data": [{"agent_run_id": "run-1", "status": "queued"}]}
            )()

    class _FakeClient:
        def table(self, table_name: str):
            query_state["table_name"] = table_name
            return _FakeQuery()

    rows = await module._fetch_run_rows(
        _FakeClient(),
        agent_run_ids=["run-1", "run-2"],
    )

    assert rows == [{"agent_run_id": "run-1", "status": "queued"}]
    assert query_state["table_name"] == "agent_runs"
    assert query_state["select_fields"] == "*"
    assert query_state["in_field"] == "agent_run_id"
    assert query_state["in_values"] == ["run-1", "run-2"]
    assert query_state["execute_calls"] == 1


@pytest.mark.asyncio
async def test_admit_runs_schedules_multiple_admissions_concurrently(monkeypatch):
    module = _load_harness_module()
    inflight = 0
    max_inflight = 0
    thread_counter = 0
    run_counter = 0

    async def _fake_get_client():
        return object()

    async def _fake_insert_thread(_client, *, account_id: str, project_id: str):
        nonlocal inflight, max_inflight, thread_counter
        assert account_id == "account-1"
        assert project_id == "project-1"
        thread_counter += 1
        inflight += 1
        max_inflight = max(max_inflight, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return f"thread-{thread_counter}"

    async def _fake_admit_queued_regular_run(**_kwargs):
        nonlocal run_counter
        run_counter += 1
        await asyncio.sleep(0.01)
        return {"agent_run_id": f"run-{run_counter}"}

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_insert_thread", _fake_insert_thread)
    monkeypatch.setattr(
        module.regular_run_admission,
        "admit_queued_regular_run",
        _fake_admit_queued_regular_run,
    )

    admitted_runs = await module._admit_runs(
        fixtures={
            "account_id": "account-1",
            "projects": [{"project_id": "project-1", "project_name": "project-1"}],
        },
        batch_id="batch-1",
        target_active_runs=4,
        active_duration_seconds=20.0,
        artifact_run_count=2,
    )

    assert len(admitted_runs) == 4
    assert max_inflight >= 2


@pytest.mark.asyncio
async def test_admit_runs_uses_distinct_project_fixtures_for_multi_sandbox_mode(
    monkeypatch,
):
    module = _load_harness_module()
    observed_project_ids: list[str] = []
    thread_counter = 0
    run_counter = 0

    async def _fake_get_client():
        return object()

    async def _fake_insert_thread(_client, *, account_id: str, project_id: str):
        nonlocal thread_counter
        assert account_id == "account-1"
        observed_project_ids.append(project_id)
        thread_counter += 1
        return f"thread-{thread_counter}"

    async def _fake_admit_queued_regular_run(**_kwargs):
        nonlocal run_counter
        run_counter += 1
        return {"agent_run_id": f"run-{run_counter}"}

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_insert_thread", _fake_insert_thread)
    monkeypatch.setattr(
        module.regular_run_admission,
        "admit_queued_regular_run",
        _fake_admit_queued_regular_run,
    )

    admitted_runs = await module._admit_runs(
        fixtures={
            "account_id": "account-1",
            "projects": [
                {"project_id": "project-1", "project_name": "project-1"},
                {"project_id": "project-2", "project_name": "project-2"},
                {"project_id": "project-3", "project_name": "project-3"},
            ],
        },
        batch_id="batch-1",
        target_active_runs=3,
        active_duration_seconds=20.0,
        artifact_run_count=0,
        sandbox_distribution_profile="per_run_project_multi_sandbox",
    )

    assert observed_project_ids == ["project-1", "project-2", "project-3"]
    assert [run["project_id"] for run in admitted_runs] == [
        "project-1",
        "project-2",
        "project-3",
    ]


@pytest.mark.asyncio
async def test_admit_runs_tracks_late_successes_after_partial_failure(monkeypatch):
    module = _load_harness_module()
    tracked_runs: list[dict[str, str | None]] = []
    thread_counter = 0

    async def _fake_get_client():
        return object()

    async def _fake_insert_thread(_client, *, account_id: str, project_id: str):
        nonlocal thread_counter
        assert account_id == "account-1"
        assert project_id == "project-1"
        thread_counter += 1
        return f"thread-{thread_counter}"

    async def _fake_admit_queued_regular_run(*, thread_id: str, **_kwargs):
        if thread_id == "thread-2":
            await asyncio.sleep(0.0)
            raise RuntimeError("queue depth exhausted")
        if thread_id == "thread-1":
            await asyncio.sleep(0.02)
        else:
            await asyncio.sleep(0.01)
        return {"agent_run_id": f"run-for-{thread_id}"}

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_insert_thread", _fake_insert_thread)
    monkeypatch.setattr(
        module.regular_run_admission,
        "admit_queued_regular_run",
        _fake_admit_queued_regular_run,
    )

    with pytest.raises(RuntimeError, match="queue depth exhausted"):
        await module._admit_runs(
            fixtures={
                "account_id": "account-1",
                "projects": [{"project_id": "project-1", "project_name": "project-1"}],
            },
            batch_id="batch-1",
            target_active_runs=4,
            active_duration_seconds=20.0,
            artifact_run_count=0,
            tracked_runs=tracked_runs,
        )

    assert tracked_runs == [
        {
            "thread_id": "thread-1",
            "agent_run_id": "run-for-thread-1",
            "project_id": "project-1",
            "artifact_path": None,
        },
        {
            "thread_id": "thread-2",
            "agent_run_id": None,
            "project_id": "project-1",
            "artifact_path": None,
        },
        {
            "thread_id": "thread-3",
            "agent_run_id": "run-for-thread-3",
            "project_id": "project-1",
            "artifact_path": None,
        },
        {
            "thread_id": "thread-4",
            "agent_run_id": "run-for-thread-4",
            "project_id": "project-1",
            "artifact_path": None,
        },
    ]


@pytest.mark.asyncio
async def test_admit_runs_uses_ramp_admission_waves_for_target_16(monkeypatch):
    module = _load_harness_module()
    thread_counter = 0
    in_flight = 0
    peak_in_flight = 0

    async def _fake_get_client():
        return object()

    async def _fake_insert_thread(_client, *, account_id: str, project_id: str):
        nonlocal thread_counter
        assert account_id == "account-1"
        assert project_id == "project-1"
        thread_counter += 1
        return f"thread-{thread_counter}"

    async def _fake_admit_queued_regular_run(*, thread_id: str, **_kwargs):
        nonlocal in_flight, peak_in_flight
        assert thread_id.startswith("thread-")
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return {"agent_run_id": f"run-for-{thread_id}"}

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_insert_thread", _fake_insert_thread)
    monkeypatch.setattr(
        module.regular_run_admission,
        "admit_queued_regular_run",
        _fake_admit_queued_regular_run,
    )

    admitted_runs = await module._admit_runs(
        fixtures={
            "account_id": "account-1",
            "projects": [{"project_id": "project-1", "project_name": "project-1"}],
        },
        batch_id="batch-1",
        target_active_runs=16,
        active_duration_seconds=20.0,
        artifact_run_count=0,
    )

    assert len(admitted_runs) == 16
    assert peak_in_flight == 4


@pytest.mark.asyncio
async def test_admit_runs_uses_nominal_target_for_ramp_wave_size_when_stop_sampling_overadmits(
    monkeypatch,
):
    module = _load_harness_module()
    thread_counter = 0
    in_flight = 0
    peak_in_flight = 0

    async def _fake_get_client():
        return object()

    async def _fake_insert_thread(_client, *, account_id: str, project_id: str):
        nonlocal thread_counter
        assert account_id == "account-1"
        assert project_id == "project-1"
        thread_counter += 1
        return f"thread-{thread_counter}"

    async def _fake_admit_queued_regular_run(*, thread_id: str, **_kwargs):
        nonlocal in_flight, peak_in_flight
        assert thread_id.startswith("thread-")
        in_flight += 1
        peak_in_flight = max(peak_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return {"agent_run_id": f"run-for-{thread_id}"}

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_insert_thread", _fake_insert_thread)
    monkeypatch.setattr(
        module.regular_run_admission,
        "admit_queued_regular_run",
        _fake_admit_queued_regular_run,
    )

    admitted_runs = await module._admit_runs(
        fixtures={
            "account_id": "account-1",
            "projects": [{"project_id": "project-1", "project_name": "project-1"}],
        },
        batch_id="batch-1",
        target_active_runs=18,
        metadata_target_tier=16,
        active_duration_seconds=20.0,
        artifact_run_count=0,
    )

    assert len(admitted_runs) == 18
    assert peak_in_flight == 4


@pytest.mark.asyncio
async def test_run_tier_cleans_up_batch_when_downstream_step_fails(monkeypatch):
    module = _load_harness_module()
    cleanup_calls: list[dict[str, object]] = []

    async def _fake_create_fixtures(
        *,
        batch_id: str,
        account_id: str,
        target_active_runs: int,
        sandbox_distribution_profile: str,
    ):
        assert target_active_runs == 27
        assert sandbox_distribution_profile == "shared_project_single_sandbox"
        return {
            "batch_id": batch_id,
            "account_id": account_id,
            "projects": [
                {
                    "project_id": "project-1",
                    "project_name": "phase3-task5b-batch-1",
                }
            ],
        }

    async def _fake_admit_runs(**_kwargs):
        return [{"agent_run_id": "run-1", "thread_id": "thread-1"}]

    async def _fake_cleanup_batch(
        *, fixtures: dict[str, object], admitted_runs: list[dict[str, object]]
    ):
        cleanup_calls.append(
            {
                "fixtures": fixtures,
                "admitted_runs": admitted_runs,
            }
        )

    async def _fake_load_supervisor_counter_snapshots():
        return {}

    async def _fake_wait_for_target_active(**_kwargs):
        return {
            "time_to_target_active_ms": 1.0,
            "peak_active_slots": 1,
            "p95_run_start_delay_ms": 2.0,
        }

    async def _fake_maybe_disturb_one_supervisor(**_kwargs):
        return {"enabled": False}

    async def _fake_issue_stop_sample(**_kwargs):
        return {"p95_stop_latency_ms": 3.0}

    monkeypatch.setattr(module, "resolve_account_id", lambda **_kwargs: "account-1")
    monkeypatch.setattr(module, "_create_fixtures", _fake_create_fixtures)
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_snapshots",
        _fake_load_supervisor_counter_snapshots,
    )
    monkeypatch.setattr(module, "_admit_runs", _fake_admit_runs)
    monkeypatch.setattr(module, "_wait_for_target_active", _fake_wait_for_target_active)
    monkeypatch.setattr(
        module, "_maybe_disturb_one_supervisor", _fake_maybe_disturb_one_supervisor
    )
    monkeypatch.setattr(module, "_issue_stop_sample", _fake_issue_stop_sample)

    async def _boom(**_kwargs):
        raise RuntimeError("terminal wait failed")

    monkeypatch.setattr(module, "_wait_for_terminal_convergence", _boom)
    monkeypatch.setattr(module, "_cleanup_batch", _fake_cleanup_batch)

    with pytest.raises(RuntimeError, match="terminal wait failed"):
        await module.run_tier(target_active_runs=25)

    assert cleanup_calls == [
        {
            "fixtures": {
                "batch_id": cleanup_calls[0]["fixtures"]["batch_id"],
                "account_id": "account-1",
                "projects": [
                    {
                        "project_id": "project-1",
                        "project_name": "phase3-task5b-batch-1",
                    }
                ],
            },
            "admitted_runs": [{"agent_run_id": "run-1", "thread_id": "thread-1"}],
        }
    ]


@pytest.mark.asyncio
async def test_run_tier_stop_sampled_overadmits_and_samples_queued_then_running_stops_around_target_active_wait(
    monkeypatch,
):
    module = _load_harness_module()
    observed_order: list[str] = []
    observed_admit_target: list[int] = []
    stop_calls: list[tuple[int, int]] = []

    async def _fake_create_fixtures(
        *,
        batch_id: str,
        account_id: str,
        target_active_runs: int,
        sandbox_distribution_profile: str,
    ):
        assert batch_id
        assert account_id == "account-1"
        assert target_active_runs == 27
        assert sandbox_distribution_profile == "shared_project_single_sandbox"
        return {
            "batch_id": batch_id,
            "account_id": account_id,
            "projects": [
                {
                    "project_id": "project-1",
                    "project_name": "phase3-task5b-batch-1",
                }
            ],
        }

    async def _fake_admit_runs(**kwargs):
        observed_order.append("admit")
        observed_admit_target.append(int(kwargs["target_active_runs"]))
        return [
            {
                "agent_run_id": f"run-{index}",
                "thread_id": f"thread-{index}",
                "project_id": "project-1",
                "artifact_path": None,
            }
            for index in range(1, 28)
        ]

    async def _fake_issue_stop_sample(**kwargs):
        stop_calls.append((kwargs["queued_stop_count"], kwargs["running_stop_count"]))
        observed_order.append(f"stop-{len(stop_calls)}")
        assert len(kwargs["batch_runs"]) == 27
        if len(stop_calls) == 1:
            assert stop_calls[-1] == (2, 0)
            return {
                "p95_stop_latency_ms": 120.0,
                "stopped_runs": 2,
                "stopped_agent_run_ids": ["run-26", "run-27"],
            }
        assert stop_calls[-1] == (0, 3)
        return {
            "p95_stop_latency_ms": 150.0,
            "stopped_runs": 3,
            "stopped_agent_run_ids": ["run-1", "run-2", "run-3"],
        }

    async def _fake_wait_for_target_active(**kwargs):
        observed_order.append("wait")
        assert observed_order == ["admit", "stop-1", "wait"]
        assert len(kwargs["agent_run_ids"]) == 27
        assert kwargs["target_active_runs"] == 25
        return {
            "time_to_target_active_ms": 1.0,
            "peak_active_slots": 25,
            "p95_run_start_delay_ms": 1.0,
        }

    async def _fake_wait_for_terminal_convergence(**_kwargs):
        return {
            "successful_completion_ratio": 1.0,
            "p95_terminal_convergence_ms": 1.0,
        }

    async def _fake_verify_artifact_visibility(**kwargs):
        assert kwargs["excluded_agent_run_ids"] == {
            "run-1",
            "run-2",
            "run-3",
            "run-26",
            "run-27",
        }
        return {"artifact_visibility_ratio": 1.0}

    async def _fake_load_supervisor_counter_snapshots():
        return {}

    async def _fake_load_supervisor_counter_deltas(_baseline):
        return {}

    async def _fake_maybe_disturb_one_supervisor(**_kwargs):
        return {"enabled": False}

    async def _fake_cleanup_batch(**_kwargs):
        return None

    monkeypatch.setattr(module, "resolve_account_id", lambda **_kwargs: "account-1")
    monkeypatch.setattr(module, "_create_fixtures", _fake_create_fixtures)
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_snapshots",
        _fake_load_supervisor_counter_snapshots,
    )
    monkeypatch.setattr(module, "_admit_runs", _fake_admit_runs)
    monkeypatch.setattr(module, "_issue_stop_sample", _fake_issue_stop_sample)
    monkeypatch.setattr(module, "_wait_for_target_active", _fake_wait_for_target_active)
    monkeypatch.setattr(
        module,
        "_wait_for_terminal_convergence",
        _fake_wait_for_terminal_convergence,
    )
    monkeypatch.setattr(
        module,
        "_verify_artifact_visibility",
        _fake_verify_artifact_visibility,
    )
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_deltas",
        _fake_load_supervisor_counter_deltas,
    )
    monkeypatch.setattr(
        module,
        "_maybe_disturb_one_supervisor",
        _fake_maybe_disturb_one_supervisor,
    )
    monkeypatch.setattr(module, "_cleanup_batch", _fake_cleanup_batch)

    result = await module.run_tier(
        target_active_runs=25,
        validation_profile="stop_sampled",
    )

    assert result["target_active_runs"] == 25
    assert observed_admit_target == [27]
    assert observed_order == ["admit", "stop-1", "wait", "stop-2"]


@pytest.mark.asyncio
async def test_run_tier_stop_sampled_multi_sandbox_creates_fixture_capacity_for_queued_stop_budget_and_starts_active_timer_before_admission(
    monkeypatch,
):
    module = _load_harness_module()
    observed_create_fixture_targets: list[int] = []
    observed_admit_targets: list[tuple[int, int | None]] = []
    observed_active_wait_start: list[float] = []

    async def _fake_get_client():
        return object()

    async def _fake_create_fixtures(
        *,
        batch_id: str,
        account_id: str,
        target_active_runs: int,
        sandbox_distribution_profile: str,
    ):
        assert batch_id
        assert account_id == "account-1"
        assert sandbox_distribution_profile == "per_run_project_multi_sandbox"
        observed_create_fixture_targets.append(target_active_runs)
        return {
            "batch_id": batch_id,
            "account_id": account_id,
            "projects": [
                {
                    "project_id": f"project-{index}",
                    "project_name": f"project-{index}",
                }
                for index in range(1, target_active_runs + 1)
            ],
        }

    async def _fake_admit_runs(**kwargs):
        observed_admit_targets.append(
            (int(kwargs["target_active_runs"]), kwargs.get("metadata_target_tier"))
        )
        return [
            {
                "agent_run_id": f"run-{index}",
                "thread_id": f"thread-{index}",
                "project_id": f"project-{index}",
                "artifact_path": None,
            }
            for index in range(1, 19)
        ]

    async def _fake_issue_stop_sample(**kwargs):
        return {
            "p95_stop_latency_ms": None,
            "stopped_runs": 0,
            "stopped_agent_run_ids": [],
            "latencies_ms": [],
        }

    async def _fake_wait_for_target_active(**kwargs):
        observed_active_wait_start.append(float(kwargs["admission_started_at"]))
        return {
            "time_to_target_active_ms": 1.0,
            "peak_active_slots": 16,
            "p95_run_start_delay_ms": 1.0,
        }

    async def _fake_wait_for_terminal_convergence(**_kwargs):
        return {
            "successful_completion_ratio": 1.0,
            "p95_terminal_convergence_ms": 1.0,
        }

    async def _fake_verify_artifact_visibility(**_kwargs):
        return {"artifact_visibility_ratio": 1.0}

    async def _fake_load_supervisor_counter_snapshots():
        return {}

    async def _fake_load_supervisor_counter_deltas(_baseline):
        return {}

    async def _fake_maybe_disturb_one_supervisor(**_kwargs):
        return {"enabled": False}

    async def _fake_cleanup_batch(**_kwargs):
        return None

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "resolve_account_id", lambda **_kwargs: "account-1")
    monkeypatch.setattr(module, "_create_fixtures", _fake_create_fixtures)
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_snapshots",
        _fake_load_supervisor_counter_snapshots,
    )
    monkeypatch.setattr(module, "_admit_runs", _fake_admit_runs)
    monkeypatch.setattr(module, "_issue_stop_sample", _fake_issue_stop_sample)
    monkeypatch.setattr(module, "_wait_for_target_active", _fake_wait_for_target_active)
    monkeypatch.setattr(
        module,
        "_wait_for_terminal_convergence",
        _fake_wait_for_terminal_convergence,
    )
    monkeypatch.setattr(
        module,
        "_verify_artifact_visibility",
        _fake_verify_artifact_visibility,
    )
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_deltas",
        _fake_load_supervisor_counter_deltas,
    )
    monkeypatch.setattr(
        module,
        "_maybe_disturb_one_supervisor",
        _fake_maybe_disturb_one_supervisor,
    )
    monkeypatch.setattr(module, "_cleanup_batch", _fake_cleanup_batch)
    monkeypatch.setattr(module.time, "monotonic", lambda: 123.0)

    result = await module.run_tier(
        target_active_runs=16,
        validation_profile="stop_sampled",
        sandbox_distribution_profile="per_run_project_multi_sandbox",
    )

    assert result["target_active_runs"] == 16
    assert observed_create_fixture_targets == [18]
    assert observed_admit_targets == [(18, 16)]
    assert observed_active_wait_start == [123.0]


@pytest.mark.asyncio
async def test_cleanup_batch_deletes_all_fixture_projects_for_multi_sandbox_mode(
    monkeypatch,
):
    module = _load_harness_module()
    delete_calls: list[tuple[str, str, str]] = []

    class _DeleteQuery:
        def __init__(self, table_name: str, field: str, value: str):
            self.table_name = table_name
            self.field = field
            self.value = value

        async def delete(self):
            delete_calls.append((self.table_name, self.field, self.value))
            return type("Result", (), {"data": [{"ok": True}]})()

    class _Table:
        def __init__(self, table_name: str):
            self.table_name = table_name

        def eq(self, field: str, value: str):
            return _DeleteQuery(self.table_name, field, value)

        def select(self, fields: str):
            if self.table_name != "projects":
                raise AssertionError(
                    "cleanup should not query project-scoped fallback when all runs are tracked"
                )

            class _ProjectSelectQuery:
                def select(self, selected_fields: str):
                    assert selected_fields == "project_id,sandbox"
                    return self

                def in_(self, field: str, values: list[str]):
                    assert field == "project_id"
                    assert values == ["project-1", "project-2", "project-3"]
                    return self

                async def execute(self):
                    return type(
                        "Result",
                        (),
                        {
                            "data": [
                                {"project_id": "project-1", "sandbox": None},
                                {"project_id": "project-2", "sandbox": None},
                                {"project_id": "project-3", "sandbox": None},
                            ]
                        },
                    )()

            query = _ProjectSelectQuery()
            return query.select(fields)

    class _Client:
        def table(self, table_name: str):
            return _Table(table_name)

    async def _fake_get_client():
        return _Client()

    monkeypatch.setattr(module, "_get_client", _fake_get_client)

    await module._cleanup_batch(
        fixtures={
            "projects": [
                {"project_id": "project-1", "project_name": "project-1"},
                {"project_id": "project-2", "project_name": "project-2"},
                {"project_id": "project-3", "project_name": "project-3"},
            ]
        },
        admitted_runs=[
            {
                "agent_run_id": "run-1",
                "thread_id": "thread-1",
                "project_id": "project-1",
            },
            {
                "agent_run_id": "run-2",
                "thread_id": "thread-2",
                "project_id": "project-2",
            },
        ],
    )

    assert delete_calls.count(("projects", "project_id", "project-1")) == 1
    assert delete_calls.count(("projects", "project_id", "project-2")) == 1
    assert delete_calls.count(("projects", "project_id", "project-3")) == 1


@pytest.mark.asyncio
async def test_cleanup_batch_deletes_provider_sandboxes_for_fixture_projects(
    monkeypatch,
):
    module = _load_harness_module()
    delete_calls: list[tuple[str, str, str]] = []
    sandbox_delete_calls: list[str] = []

    class _DeleteQuery:
        def __init__(self, table_name: str, field: str, value: str):
            self.table_name = table_name
            self.field = field
            self.value = value

        async def delete(self):
            delete_calls.append((self.table_name, self.field, self.value))
            return type("Result", (), {"data": [{"ok": True}]})()

    class _ProjectSelectQuery:
        def __init__(self):
            self._field = None
            self._values: list[str] = []

        def select(self, fields: str):
            assert fields == "project_id,sandbox"
            return self

        def in_(self, field: str, values: list[str]):
            self._field = field
            self._values = list(values)
            return self

        async def execute(self):
            assert self._field == "project_id"
            assert self._values == ["project-1", "project-2"]
            return type(
                "Result",
                (),
                {
                    "data": [
                        {"project_id": "project-1", "sandbox": '{"id":"sb-1","type":"code"}'},
                        {"project_id": "project-2", "sandbox": {"id": "sb-2", "type": "code"}},
                    ]
                },
            )()

    class _Table:
        def __init__(self, table_name: str):
            self.table_name = table_name

        def eq(self, field: str, value: str):
            return _DeleteQuery(self.table_name, field, value)

        def select(self, fields: str):
            if self.table_name == "projects":
                query = _ProjectSelectQuery()
                return query.select(fields)
            raise AssertionError(
                f"unexpected select for cleanup table {self.table_name}"
            )

    class _Client:
        def table(self, table_name: str):
            return _Table(table_name)

    async def _fake_get_client():
        return _Client()

    async def _fake_delete_sandbox(sandbox_id: str):
        sandbox_delete_calls.append(sandbox_id)

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "delete_sandbox", _fake_delete_sandbox, raising=False)

    await module._cleanup_batch(
        fixtures={
            "projects": [
                {"project_id": "project-1", "project_name": "project-1"},
                {"project_id": "project-2", "project_name": "project-2"},
            ]
        },
        admitted_runs=[
            {
                "agent_run_id": "run-1",
                "thread_id": "thread-1",
                "project_id": "project-1",
            },
            {
                "agent_run_id": "run-2",
                "thread_id": "thread-2",
                "project_id": "project-2",
            },
        ],
    )

    assert sandbox_delete_calls == ["sb-1", "sb-2"]
    assert ("projects", "project_id", "project-1") in delete_calls
    assert ("projects", "project_id", "project-2") in delete_calls


@pytest.mark.asyncio
async def test_cleanup_batch_deletes_project_scoped_untracked_rows(monkeypatch):
    module = _load_harness_module()
    delete_calls: list[tuple[str, str, str]] = []

    class _DeleteQuery:
        def __init__(self, table_name: str, field: str, value: str):
            self.table_name = table_name
            self.field = field
            self.value = value

        async def delete(self):
            delete_calls.append((self.table_name, self.field, self.value))
            return type("Result", (), {"data": [{"ok": True}]})()

    class _SelectQuery:
        def __init__(self, table_name: str):
            self.table_name = table_name
            self._field = None
            self._values: list[str] = []

        def select(self, fields: str):
            self._fields = fields
            return self

        def in_(self, field: str, values: list[str]):
            self._field = field
            self._values = list(values)
            return self

        async def execute(self):
            if self.table_name == "threads":
                assert self._fields == "thread_id,project_id"
                assert self._field == "project_id"
                assert self._values == ["project-1"]
                return type(
                    "Result",
                    (),
                    {"data": [{"thread_id": "thread-1", "project_id": "project-1"}]},
                )()
            if self.table_name == "projects":
                assert self._fields == "project_id,sandbox"
                assert self._field == "project_id"
                assert self._values == ["project-1"]
                return type(
                    "Result",
                    (),
                    {"data": [{"project_id": "project-1", "sandbox": None}]},
                )()
            assert self.table_name == "agent_runs"
            assert self._fields == "agent_run_id,thread_id"
            assert self._field == "thread_id"
            assert self._values == ["thread-1"]
            return type(
                "Result",
                (),
                {"data": [{"agent_run_id": "run-untracked", "thread_id": "thread-1"}]},
            )()

    class _Table:
        def __init__(self, table_name: str):
            self.table_name = table_name

        def eq(self, field: str, value: str):
            return _DeleteQuery(self.table_name, field, value)

        def select(self, fields: str):
            query = _SelectQuery(self.table_name)
            return query.select(fields)

    class _Client:
        def table(self, table_name: str):
            return _Table(table_name)

    async def _fake_get_client():
        return _Client()

    monkeypatch.setattr(module, "_get_client", _fake_get_client)

    await module._cleanup_batch(
        fixtures={
            "batch_id": "batch-1",
            "projects": [
                {"project_id": "project-1", "project_name": "project-1"},
            ],
        },
        admitted_runs=[
            {
                "thread_id": "thread-1",
                "agent_run_id": None,
                "project_id": "project-1",
                "artifact_path": None,
            }
        ],
    )

    assert ("regular_run_attempts", "agent_run_id", "run-untracked") in delete_calls
    assert ("agent_runs", "agent_run_id", "run-untracked") in delete_calls
    assert ("workspace_artifacts", "agent_run_id", "run-untracked") in delete_calls
    assert ("threads", "thread_id", "thread-1") in delete_calls
    assert ("projects", "project_id", "project-1") in delete_calls


@pytest.mark.asyncio
async def test_verify_artifact_visibility_excludes_intentionally_stopped_runs_from_denominator(
    monkeypatch,
):
    module = _load_harness_module()

    class _Query:
        def __init__(self):
            self._run_id = None
            self._path = None

        def select(self, _fields: str):
            return self

        def eq(self, field: str, value: str):
            if field == "agent_run_id":
                self._run_id = value
            elif field == "path":
                self._path = value
            return self

        async def execute(self):
            assert self._path
            data = [{"artifact_id": "artifact-1"}] if self._run_id == "run-1" else []
            return type("Result", (), {"data": data})()

    class _Client:
        def table(self, table_name: str):
            assert table_name == "workspace_artifacts"
            return _Query()

    async def _fake_get_client():
        return _Client()

    monkeypatch.setattr(module, "_get_client", _fake_get_client)

    result = await module._verify_artifact_visibility(
        batch_runs=[
            {"agent_run_id": "run-1", "artifact_path": "/workspace/a.txt"},
            {"agent_run_id": "run-2", "artifact_path": "/workspace/b.txt"},
        ],
        expected_artifact_runs=2,
        excluded_agent_run_ids={"run-2"},
    )

    assert result["artifact_visibility_ratio"] == 1.0


@pytest.mark.asyncio
async def test_verify_artifact_visibility_waits_for_delayed_workspace_artifact_visibility(
    monkeypatch,
):
    module = _load_harness_module()
    query_counts: dict[str, int] = {}

    class _Query:
        def __init__(self):
            self._run_id = None
            self._path = None

        def select(self, _fields: str):
            return self

        def eq(self, field: str, value: str):
            if field == "agent_run_id":
                self._run_id = value
            elif field == "path":
                self._path = value
            return self

        async def execute(self):
            assert self._run_id
            assert self._path
            query_counts[self._run_id] = query_counts.get(self._run_id, 0) + 1
            visible = self._run_id == "run-1" or query_counts[self._run_id] >= 2
            data = [{"artifact_id": f"artifact-{self._run_id}"}] if visible else []
            return type("Result", (), {"data": data})()

    class _Client:
        def table(self, table_name: str):
            assert table_name == "workspace_artifacts"
            return _Query()

    async def _fake_get_client():
        return _Client()

    async def _fake_sleep(_seconds: float):
        return None

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module.asyncio, "sleep", _fake_sleep)

    result = await module._verify_artifact_visibility(
        batch_runs=[
            {"agent_run_id": "run-1", "artifact_path": "/workspace/a.txt"},
            {"agent_run_id": "run-2", "artifact_path": "/workspace/b.txt"},
        ],
        expected_artifact_runs=2,
    )

    assert result["artifact_visibility_ratio"] == 1.0
    assert query_counts["run-2"] >= 2


@pytest.mark.asyncio
async def test_wait_for_terminal_convergence_uses_per_run_p95_not_slowest_tail(
    monkeypatch,
):
    module = _load_harness_module()
    agent_run_ids = [f"run-{index}" for index in range(20)]
    fetch_count = 0

    async def _fake_get_client():
        return object()

    async def _fake_fetch_run_rows(_client, *, agent_run_ids: list[str]):
        nonlocal fetch_count
        fetch_count += 1
        if fetch_count == 1:
            return [
                {
                    "agent_run_id": agent_run_id,
                    "status": "completed" if index < 19 else "running",
                }
                for index, agent_run_id in enumerate(agent_run_ids)
            ]
        return [
            {
                "agent_run_id": agent_run_id,
                "status": "completed",
            }
            for agent_run_id in agent_run_ids
        ]

    async def _fake_sleep(_seconds: float):
        return None

    monotonic_values = iter([0.0, 0.0, 0.0, 10.0, 10.0, 10.0])

    def _fake_monotonic() -> float:
        return next(monotonic_values, 10.0)

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_fetch_run_rows", _fake_fetch_run_rows)
    monkeypatch.setattr(module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(module.time, "monotonic", _fake_monotonic)

    result = await module._wait_for_terminal_convergence(agent_run_ids=agent_run_ids)

    assert result["successful_completion_ratio"] == 1.0
    assert result["p95_terminal_convergence_ms"] == 0.0


@pytest.mark.asyncio
async def test_wait_for_target_active_uses_explicit_admission_started_at_for_latency(
    monkeypatch,
):
    module = _load_harness_module()

    async def _fake_get_client():
        return object()

    async def _fake_fetch_attempt_rows(_client, *, agent_run_ids: list[str]):
        assert agent_run_ids == ["run-1", "run-2"]
        return [
            {
                "agent_run_id": "run-1",
                "status": "running",
                "queued_at": None,
                "running_at": None,
            },
            {
                "agent_run_id": "run-2",
                "status": "running",
                "queued_at": None,
                "running_at": None,
            },
        ]

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_fetch_attempt_rows", _fake_fetch_attempt_rows)
    monkeypatch.setattr(module.time, "monotonic", lambda: 15.0)

    result = await module._wait_for_target_active(
        agent_run_ids=["run-1", "run-2"],
        target_active_runs=2,
        admission_started_at=10.0,
    )

    assert result["time_to_target_active_ms"] == 5000.0


@pytest.mark.asyncio
async def test_wait_for_terminal_convergence_requires_all_requested_runs(monkeypatch):
    module = _load_harness_module()

    async def _fake_get_client():
        return object()

    async def _fake_fetch_run_rows(_client, *, agent_run_ids):
        assert agent_run_ids == ["run-1", "run-2"]
        return [{"agent_run_id": "run-1", "status": "completed"}]

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "_fetch_run_rows", _fake_fetch_run_rows)

    with pytest.raises(TimeoutError, match="terminal convergence"):
        await module._wait_for_terminal_convergence(
            agent_run_ids=["run-1", "run-2"],
            poll_interval_seconds=0.0,
            timeout_seconds=0.01,
        )


@pytest.mark.asyncio
async def test_run_tier_cleans_up_tracked_batch_after_partial_admission_failure(
    monkeypatch,
):
    module = _load_harness_module()
    cleaned_batches: list[tuple[dict[str, object], list[dict[str, str | None]]]] = []
    fixtures = {
        "batch_id": "batch-1",
        "account_id": "account-1",
        "projects": [
            {
                "project_id": "project-1",
                "project_name": "phase3-task5b-batch-1",
            }
        ],
    }

    async def _fake_get_client():
        return object()

    async def _fake_create_fixtures(
        *,
        batch_id: str,
        account_id: str,
        target_active_runs: int,
        sandbox_distribution_profile: str,
    ):
        assert batch_id
        assert account_id == "account-1"
        assert target_active_runs == 27
        assert sandbox_distribution_profile == "shared_project_single_sandbox"
        return fixtures

    async def _fake_load_supervisor_counter_snapshots():
        return {}

    async def _fake_admit_runs(**kwargs):
        tracked_runs = kwargs["tracked_runs"]
        tracked_runs.extend(
            [
                {
                    "thread_id": "thread-1",
                    "agent_run_id": "run-1",
                    "project_id": "project-1",
                    "artifact_path": None,
                },
                {
                    "thread_id": "thread-2",
                    "agent_run_id": None,
                    "project_id": "project-1",
                    "artifact_path": None,
                },
            ]
        )
        raise RuntimeError("admission boom")

    async def _fake_cleanup_batch(*, fixtures, admitted_runs):
        cleaned_batches.append((fixtures, [dict(run) for run in admitted_runs]))

    monkeypatch.setattr(module, "_get_client", _fake_get_client)
    monkeypatch.setattr(module, "resolve_account_id", lambda **_kwargs: "account-1")
    monkeypatch.setattr(module, "_create_fixtures", _fake_create_fixtures)
    monkeypatch.setattr(
        module,
        "_load_supervisor_counter_snapshots",
        _fake_load_supervisor_counter_snapshots,
    )
    monkeypatch.setattr(module, "_admit_runs", _fake_admit_runs)
    monkeypatch.setattr(module, "_cleanup_batch", _fake_cleanup_batch)

    with pytest.raises(RuntimeError, match="admission boom"):
        await module.run_tier(target_active_runs=25)

    assert cleaned_batches == [
        (
            fixtures,
            [
                {
                    "thread_id": "thread-1",
                    "agent_run_id": "run-1",
                    "project_id": "project-1",
                    "artifact_path": None,
                },
                {
                    "thread_id": "thread-2",
                    "agent_run_id": None,
                    "project_id": "project-1",
                    "artifact_path": None,
                },
            ],
        )
    ]
