import importlib
import os
from pathlib import Path


START_WORKTREE_DEV_PATH = Path(__file__).resolve().parents[3] / "start_worktree_dev.sh"
STOP_WORKTREE_DEV_PATH = Path(__file__).resolve().parents[3] / "stop_worktree_dev.sh"
START_WILLIAM_PROD_PATH = Path(__file__).resolve().parents[3] / "start_william_prod.sh"
API_PATH = Path(__file__).resolve().parents[1] / "api.py"
RUN_AGENT_BACKGROUND_PATH = Path(__file__).resolve().parents[1] / "run_agent_background.py"
AGENT_RUN_PATH = Path(__file__).resolve().parents[1] / "agent" / "run.py"
CONFIG_PATH = Path(__file__).resolve().parents[1] / "utils" / "config.py"
SANDBOX_PATH = Path(__file__).resolve().parents[1] / "sandbox" / "sandbox.py"
MODEL_RESOLVER_PATH = Path(__file__).resolve().parents[1] / "utils" / "model_resolver.py"
QUEUE_NAMES_PATH = (
    Path(__file__).resolve().parents[1] / "utils" / "dramatiq_queue_names.py"
)
REGULAR_SUPERVISOR_BACKGROUND_PATH = (
    Path(__file__).resolve().parents[1] / "regular_supervisor_background.py"
)


def test_start_worktree_dev_exports_runtime_tokenized_dramatiq_queues():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'export WORKTREE_RUNTIME_TOKEN=' in script
    assert "printf 'redis_db=%s\\n' \"${REDIS_DB:-}\"" in script
    assert "printf 'model_to_use=%s\\n' \"${MODEL_TO_USE:-}\"" in script
    assert 'run_agent_background_worktree_${BACKEND_PORT}_${WORKTREE_RUNTIME_TOKEN}' in script
    assert 'sandbox_cleanup_worktree_${BACKEND_PORT}_${WORKTREE_RUNTIME_TOKEN}' in script
    assert 'shadow_clone_subagents_worktree_${BACKEND_PORT}_${WORKTREE_RUNTIME_TOKEN}' in script
    assert 'regular_supervisor_worktree_${BACKEND_PORT}_${WORKTREE_RUNTIME_TOKEN}' in script


def test_start_william_prod_forces_agentscope_and_disables_remote_prompt_fetch():
    script = START_WILLIAM_PROD_PATH.read_text(encoding="utf-8")

    assert 'load_backend_env_if_present' in script
    assert 'export AGENT_BACKEND="agentscope"' in script
    assert 'export DISABLE_LANGFUSE_PROMPT_FETCH="${DISABLE_LANGFUSE_PROMPT_FETCH:-1}"' in script
    assert 'export DISABLE_LANGFUSE_PROMPT_FETCH=$(printf' in script


def test_start_william_prod_defaults_regular_supervisor_capacity_to_48():
    script = START_WILLIAM_PROD_PATH.read_text(encoding="utf-8")

    assert "ensure_min_int_env REGULAR_SUPERVISOR_PROCESSES 6" in script
    assert "ensure_min_int_env REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS 8" in script
    assert "ensure_min_int_env REGULAR_QUEUE_MAX_DEPTH 96" in script
    assert (
        "ensure_min_int_env AGENTSCOPE_SERVER_CONCURRENCY_BUDGET "
        '"$regular_supervisor_weighted_capacity"' in script
    )
    assert (
        "ensure_min_int_env AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET "
        '"$regular_supervisor_user_capacity"' in script
    )
    assert 'export SHADOW_CLONE_V2_EXECUTION_CHAIN="${SHADOW_CLONE_V2_EXECUTION_CHAIN:-regular_supervisor}"' in script
    assert 'export SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS="${SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS:-10}"' in script
    assert 'export REGULAR_SUPERVISOR_PROCESSES="${REGULAR_SUPERVISOR_PROCESSES:-6}"' in script
    assert 'export REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS="${REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS:-8}"' in script
    assert 'regular_supervisor_user_capacity="$((REGULAR_SUPERVISOR_PROCESSES * REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS))"' in script
    assert 'regular_supervisor_weighted_capacity="$((regular_supervisor_user_capacity * AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST))"' in script
    assert 'export AGENTSCOPE_SERVER_CONCURRENCY_BUDGET="${AGENTSCOPE_SERVER_CONCURRENCY_BUDGET:-$regular_supervisor_weighted_capacity}"' in script
    assert 'export AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET="${AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET:-$regular_supervisor_user_capacity}"' in script
    assert 'export REGULAR_QUEUE_MAX_DEPTH="${REGULAR_QUEUE_MAX_DEPTH:-96}"' in script
    assert 'export SHADOW_CLONE_V2_EXECUTION_CHAIN=$(printf' in script
    assert 'export SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS=$(printf' in script


def test_start_worktree_dev_preserves_existing_shell_environment():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'load_env_defaults .env.development' in script
    assert 'eval "$(python3 "$LOAD_ENV_PY" .env.development)"' not in script


def test_start_worktree_dev_exports_concurrency_sizing_defaults():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'LEGACY_QWEN_DEFAULT_MODEL="dashscope/qwen3.5-397b-a17b"' in script
    assert 'DEFAULT_REGULAR_MODEL="openrouter/minimax/minimax-m2.5"' in script
    assert 'if [ -z "${MODEL_TO_USE:-}" ] || [ "${MODEL_TO_USE}" = "$LEGACY_QWEN_DEFAULT_MODEL" ]; then' in script
    assert 'export MODEL_TO_USE="$DEFAULT_REGULAR_MODEL"' in script
    assert 'export REGULAR_EXECUTION_MODE="${REGULAR_EXECUTION_MODE:-phase2_supervisor}"' in script
    assert 'export REDIS_MAX_CONNECTIONS="${REDIS_MAX_CONNECTIONS:-64}"' in script
    assert 'export PG_POOL_MIN_SIZE="${PG_POOL_MIN_SIZE:-2}"' in script
    assert 'export PG_POOL_MAX_SIZE="${PG_POOL_MAX_SIZE:-30}"' in script
    assert 'export AGENTSCOPE_SERVER_CONCURRENCY_BUDGET="${AGENTSCOPE_SERVER_CONCURRENCY_BUDGET:-$AUTO_SERVER_CONCURRENCY_BUDGET}"' in script
    assert 'export AGENTSCOPE_SERVER_REGULAR_RUN_COST="${AGENTSCOPE_SERVER_REGULAR_RUN_COST:-1}"' in script
    assert 'if [ -z "${AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST:-}" ]; then' in script
    assert 'if [ "${SHADOW_CLONE_SUBAGENT_EXECUTION_MODE}" = "local" ]; then' in script
    assert 'export AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST="2"' in script
    assert 'export AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST="3"' in script
    assert 'if [ -z "${AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET:-}" ]; then' in script
    assert 'DEFAULT_RESERVED_SHADOW_CLONE_BUDGET="$((AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST * 2))"' in script
    assert 'DEFAULT_REGULAR_ADMISSION_BUDGET="$((AUTO_SERVER_CONCURRENCY_BUDGET - DEFAULT_RESERVED_SHADOW_CLONE_BUDGET))"' in script
    assert 'export AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET="$DEFAULT_REGULAR_ADMISSION_BUDGET"' in script
    assert 'export SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL="${SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL:-24}"' in script
    assert 'export SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX="${SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX:-10}"' in script
    assert 'export SANDBOX_SHARED_LEASE_TTL_SECONDS="${SANDBOX_SHARED_LEASE_TTL_SECONDS:-120}"' in script
    assert 'REGULAR_SUPERVISOR_PROCESSES="${REGULAR_SUPERVISOR_PROCESSES:-4}"' in script
    assert 'REGULAR_SUPERVISOR_THREADS="${REGULAR_SUPERVISOR_THREADS:-1}"' in script
    assert 'export REGULAR_SUPERVISOR_PERSISTENT="${REGULAR_SUPERVISOR_PERSISTENT:-true}"' in script
    assert 'export REGULAR_SUPERVISOR_SHARD_PREFIX="${REGULAR_SUPERVISOR_SHARD_PREFIX:-regular-supervisor-worktree_${BACKEND_PORT}_${WORKTREE_RUNTIME_TOKEN}}"' in script
    assert 'export REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS="${REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS:-4}"' in script
    assert 'export REGULAR_QUEUE_MAX_DEPTH="${REGULAR_QUEUE_MAX_DEPTH:-32}"' in script
    assert 'export REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH="${REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH:-$REGULAR_QUEUE_MAX_DEPTH}"' in script


def test_start_worktree_dev_exports_sandbox_create_cold_start_defaults():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'export SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL="${SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL:-4}"' in script
    assert 'export SANDBOX_CREATE_QUEUE_TIMEOUT_SECONDS="${SANDBOX_CREATE_QUEUE_TIMEOUT_SECONDS:-180}"' in script
    assert 'export SANDBOX_CREATE_LEASE_TTL_SECONDS="${SANDBOX_CREATE_LEASE_TTL_SECONDS:-240}"' in script
    assert 'export SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS="${SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS:-0.5}"' in script
    assert 'export SANDBOX_CREATE_RETRY_MAX_ATTEMPTS="${SANDBOX_CREATE_RETRY_MAX_ATTEMPTS:-4}"' in script
    assert 'export SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS="${SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS:-0.5}"' in script
    assert 'export SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS="${SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS:-8}"' in script


def test_start_worktree_dev_echoes_regular_execution_mode():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'echo "✓ REGULAR_EXECUTION_MODE is set to: $REGULAR_EXECUTION_MODE"' in script
    assert 'echo "✓ REGULAR_SUPERVISOR_PERSISTENT is set to: $REGULAR_SUPERVISOR_PERSISTENT"' in script
    assert 'echo "✓ REGULAR_SUPERVISOR_SHARD_PREFIX is set to: $REGULAR_SUPERVISOR_SHARD_PREFIX"' in script


def test_start_worktree_dev_exports_regular_load_harness_flags():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'export REGULAR_LOAD_HARNESS_STUB_MODE="${REGULAR_LOAD_HARNESS_STUB_MODE:-false}"' in script
    assert 'export REGULAR_LOAD_HARNESS_METRICS_ENABLED="${REGULAR_LOAD_HARNESS_METRICS_ENABLED:-false}"' in script
    assert 'echo "✓ REGULAR_LOAD_HARNESS_STUB_MODE is set to: $REGULAR_LOAD_HARNESS_STUB_MODE"' in script
    assert 'echo "✓ REGULAR_LOAD_HARNESS_METRICS_ENABLED is set to: $REGULAR_LOAD_HARNESS_METRICS_ENABLED"' in script


def test_start_worktree_dev_starts_dedicated_regular_supervisor_worker():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'REGULAR_SUPERVISOR_LOG="$LOGS_DIR/regular_supervisor.log"' in script
    assert 'REGULAR_SUPERVISOR_META_FILE="$RUNTIME_DIR/regular_supervisor.meta"' in script
    assert "Starting dedicated regular supervisor worker" in script
    assert "rotate_log \"$REGULAR_SUPERVISOR_LOG\"" in script
    assert "regular_supervisor_background" in script
    assert 'write_runtime_meta \\' in script
    assert '"regular_supervisor"' in script


def test_start_worktree_dev_places_regular_supervisor_module_before_queue_flag():
    script = START_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert (
        'setsid dramatiq --processes "$REGULAR_SUPERVISOR_PROCESSES" --threads '
        '"$REGULAR_SUPERVISOR_THREADS" regular_supervisor_background --queues '
        '"$REGULAR_SUPERVISOR_QUEUE" > "$REGULAR_SUPERVISOR_LOG" 2>&1 &'
    ) in script
    assert (
        'setsid python3 -m dramatiq --processes "$REGULAR_SUPERVISOR_PROCESSES" '
        '--threads "$REGULAR_SUPERVISOR_THREADS" regular_supervisor_background '
        '--queues "$REGULAR_SUPERVISOR_QUEUE" > "$REGULAR_SUPERVISOR_LOG" 2>&1 &'
    ) in script


def test_stop_worktree_dev_stops_dedicated_regular_supervisor_worker():
    script = STOP_WORKTREE_DEV_PATH.read_text(encoding="utf-8")

    assert 'REGULAR_SUPERVISOR_META_FILE="$RUNTIME_DIR/regular_supervisor.meta"' in script
    assert 'stop_service "Regular supervisor" "$REGULAR_SUPERVISOR_META_FILE" "$BACKEND_DIR" ""' in script


def test_dramatiq_queue_names_define_regular_supervisor_queue():
    script = QUEUE_NAMES_PATH.read_text(encoding="utf-8")

    assert "REGULAR_SUPERVISOR_QUEUE = _read_queue_name(" in script
    assert '"REGULAR_SUPERVISOR_QUEUE"' in script
    assert '"regular_supervisor"' in script


def test_regular_supervisor_background_uses_dedicated_queue_constant():
    script = REGULAR_SUPERVISOR_BACKGROUND_PATH.read_text(encoding="utf-8")

    assert "from utils.dramatiq_queue_names import REGULAR_SUPERVISOR_QUEUE" in script
    assert "def get_regular_supervisor_actor_time_limit_ms()" in script
    assert "time_limit=get_regular_supervisor_actor_time_limit_ms()" in script
    assert "time_limit=0" not in script
    assert "from run_agent_background import" not in script


def test_backend_env_example_documents_shared_sandbox_capacity_knobs():
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    contents = env_example.read_text(encoding="utf-8")

    assert "MODEL_TO_USE=openrouter/minimax/minimax-m2.5" in contents
    assert "SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL" in contents
    assert "SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX=10" in contents
    assert "SANDBOX_SHARED_LEASE_TTL_SECONDS=120" in contents
    assert "AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET=12" in contents


def test_api_dotenv_preserves_process_environment():
    script = API_PATH.read_text(encoding="utf-8")

    assert 'load_dotenv(override=False)' in script


def test_worker_entrypoints_preserve_process_environment():
    assert 'override=False' in RUN_AGENT_BACKGROUND_PATH.read_text(encoding="utf-8")
    assert 'load_dotenv(override=False)' in AGENT_RUN_PATH.read_text(encoding="utf-8")
    assert 'load_dotenv(override=False)' in CONFIG_PATH.read_text(encoding="utf-8")
    assert 'load_dotenv(override=False)' in SANDBOX_PATH.read_text(encoding="utf-8")
    assert 'override=False' in MODEL_RESOLVER_PATH.read_text(encoding="utf-8")


def test_redis_env_loader_preserves_process_environment(monkeypatch):
    redis_service = importlib.import_module("services.redis")

    monkeypatch.setenv("REDIS_HOST", "runtime-host")
    monkeypatch.setenv("REDIS_PORT", "6380")
    monkeypatch.setenv("REDIS_PASSWORD", "runtime-password")
    monkeypatch.setenv("REDIS_DB", "15")

    def fake_load_dotenv(*args, **kwargs):
        if kwargs.get("override"):
            os.environ["REDIS_HOST"] = "dotenv-host"
            os.environ["REDIS_PORT"] = "6399"
            os.environ["REDIS_PASSWORD"] = "dotenv-password"
            os.environ["REDIS_DB"] = "9"

    monkeypatch.setattr(redis_service, "load_dotenv", fake_load_dotenv)

    assert redis_service._load_redis_env() == (
        "runtime-host",
        6380,
        "runtime-password",
        15,
    )
