#!/usr/bin/env bash

set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="${WILLIAM_BACKEND_DIR:-$PROJECT_ROOT/WilliamManus/backend}"
FRONTEND_DIR="$PROJECT_ROOT/WilliamManus/frontend"
BACKEND_ENV_FILE="$BACKEND_DIR/.env.production"
LOG_DIR="$PROJECT_ROOT/logs/william_prod"

SESSION_BACKEND="wm-backend"
SESSION_RUN_AGENT="wm-dramatiq"
SESSION_REGULAR_SUPERVISOR="wm-regular-supervisor"
SESSION_FRONTEND="wm-frontend"

BACKEND_LOG="$LOG_DIR/backend_api.log"
RUN_AGENT_LOG="$LOG_DIR/run_agent_background.log"
REGULAR_SUPERVISOR_LOG="$LOG_DIR/regular_supervisor_background.log"
FRONTEND_LOG="$LOG_DIR/frontend.log"

CONDA_SH="${CONDA_SH:-$HOME/miniconda3/etc/profile.d/conda.sh}"
CONDA_ENV_NAME="${CONDA_ENV_NAME:-roysmanus}"

require_tmux() {
    if ! command -v tmux >/dev/null 2>&1; then
        echo "ERROR: tmux is required but not installed."
        exit 1
    fi
}

require_port_tools() {
    if ! command -v lsof >/dev/null 2>&1; then
        echo "ERROR: lsof is required but not installed."
        exit 1
    fi
}

require_curl() {
    if ! command -v curl >/dev/null 2>&1; then
        echo "ERROR: curl is required but not installed."
        exit 1
    fi
}

tmux_session_exists() {
    local session_name="$1"
    tmux has-session -t "$session_name" 2>/dev/null
}

list_matching_pids() {
    local pattern="$1"
    pgrep -f -- "$pattern" || true
}

ensure_no_matching_processes() {
    local pattern="$1"
    local label="$2"
    local pids

    pids="$(list_matching_pids "$pattern")"
    if [ -z "$pids" ]; then
        return 0
    fi

    echo "ERROR: found stale $label process(es)."
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        ps -fp "$pid" || true
    done <<< "$pids"
    echo "Run stop_william_prod.sh before starting again."
    exit 1
}

ensure_session_absent() {
    local session_name="$1"
    if tmux_session_exists "$session_name"; then
        echo "ERROR: tmux session '$session_name' already exists. Stop it first."
        exit 1
    fi
}

load_backend_env_if_present() {
    if [ ! -f "$BACKEND_ENV_FILE" ]; then
        return 0
    fi

    # Allow simple KEY=VALUE env files without requiring all vars to already exist.
    set +u
    set -a
    # shellcheck disable=SC1090
    . "$BACKEND_ENV_FILE"
    set +a
    set -u
}

ensure_min_int_env() {
    local name="$1"
    local minimum="$2"
    local current="${!name:-}"

    if ! [[ "$minimum" =~ ^[0-9]+$ ]]; then
        echo "ERROR: invalid minimum for $name: $minimum"
        exit 1
    fi

    if ! [[ "$current" =~ ^[0-9]+$ ]] || [ "$current" -lt "$minimum" ]; then
        export "$name=$minimum"
    fi
}

set_wrapper_defaults() {
    export NODE_ENV="${NODE_ENV:-production}"
    export BACKEND_PORT="${BACKEND_PORT:-8002}"
    export BACKEND_RELOAD="${BACKEND_RELOAD:-false}"
    export FRONTEND_PORT="${FRONTEND_PORT:-3000}"
    export PORT="${PORT:-$FRONTEND_PORT}"

    export REGULAR_EXECUTION_MODE="${REGULAR_EXECUTION_MODE:-phase2_supervisor}"
    export SHADOW_CLONE_V2_EXECUTION_CHAIN="${SHADOW_CLONE_V2_EXECUTION_CHAIN:-regular_supervisor}"
    export SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS="${SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS:-10}"
    export SHADOW_CLONE_SUBAGENT_EXECUTION_MODE="${SHADOW_CLONE_SUBAGENT_EXECUTION_MODE:-local}"
    export AGENT_BACKEND="agentscope"
    export DISABLE_LANGFUSE_PROMPT_FETCH="${DISABLE_LANGFUSE_PROMPT_FETCH:-1}"

    # Stable production queue names.
    export DRAMATIQ_RUN_AGENT_QUEUE="${DRAMATIQ_RUN_AGENT_QUEUE:-run_agent_background_prod}"
    export DRAMATIQ_SANDBOX_CLEANUP_QUEUE="${DRAMATIQ_SANDBOX_CLEANUP_QUEUE:-sandbox_cleanup_prod}"
    export SHADOW_CLONE_SUBAGENT_QUEUE="${SHADOW_CLONE_SUBAGENT_QUEUE:-shadow_clone_subagents_prod}"
    export REGULAR_SUPERVISOR_QUEUE="${REGULAR_SUPERVISOR_QUEUE:-regular_supervisor_prod}"

    export DRAMATIQ_PROCESSES="${DRAMATIQ_PROCESSES:-2}"
    export DRAMATIQ_THREADS="${DRAMATIQ_THREADS:-8}"
    export REGULAR_SUPERVISOR_PROCESSES="${REGULAR_SUPERVISOR_PROCESSES:-6}"
    export REGULAR_SUPERVISOR_THREADS="${REGULAR_SUPERVISOR_THREADS:-1}"
    export REGULAR_SUPERVISOR_PERSISTENT="${REGULAR_SUPERVISOR_PERSISTENT:-true}"
    export REGULAR_SUPERVISOR_SHARD_PREFIX="${REGULAR_SUPERVISOR_SHARD_PREFIX:-regular-supervisor-prod}"
    export REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS="${REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS:-8}"
    ensure_min_int_env REGULAR_SUPERVISOR_PROCESSES 6
    ensure_min_int_env REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS 8

    local regular_supervisor_user_capacity
    local regular_supervisor_weighted_capacity
    regular_supervisor_user_capacity="$((REGULAR_SUPERVISOR_PROCESSES * REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS))"
    export AGENTSCOPE_SERVER_REGULAR_RUN_COST="${AGENTSCOPE_SERVER_REGULAR_RUN_COST:-1}"
    if [ -z "${AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST:-}" ]; then
        if [ "${SHADOW_CLONE_SUBAGENT_EXECUTION_MODE}" = "local" ]; then
            export AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST="2"
        else
            export AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST="3"
        fi
    fi
    regular_supervisor_weighted_capacity="$((regular_supervisor_user_capacity * AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST))"
    export AGENTSCOPE_SERVER_CONCURRENCY_BUDGET="${AGENTSCOPE_SERVER_CONCURRENCY_BUDGET:-$regular_supervisor_weighted_capacity}"
    ensure_min_int_env AGENTSCOPE_SERVER_CONCURRENCY_BUDGET "$regular_supervisor_weighted_capacity"
    export AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET="${AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET:-$regular_supervisor_user_capacity}"
    ensure_min_int_env AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET "$regular_supervisor_user_capacity"
    export AGENTSCOPE_SERVER_RUN_CAPACITY_TTL_SECONDS="${AGENTSCOPE_SERVER_RUN_CAPACITY_TTL_SECONDS:-90}"

    export REGULAR_QUEUE_MAX_DEPTH="${REGULAR_QUEUE_MAX_DEPTH:-96}"
    ensure_min_int_env REGULAR_QUEUE_MAX_DEPTH 96
    export REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH="${REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH:-$REGULAR_QUEUE_MAX_DEPTH}"
    ensure_min_int_env REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH "$REGULAR_QUEUE_MAX_DEPTH"

    export SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL="${SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL:-24}"
    export SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX="${SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX:-10}"
    export SANDBOX_SHARED_LEASE_TTL_SECONDS="${SANDBOX_SHARED_LEASE_TTL_SECONDS:-120}"
    export SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL="${SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL:-4}"
    export SANDBOX_CREATE_QUEUE_TIMEOUT_SECONDS="${SANDBOX_CREATE_QUEUE_TIMEOUT_SECONDS:-180}"
    export SANDBOX_CREATE_LEASE_TTL_SECONDS="${SANDBOX_CREATE_LEASE_TTL_SECONDS:-240}"
    export SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS="${SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS:-0.5}"
    export SANDBOX_CREATE_RETRY_MAX_ATTEMPTS="${SANDBOX_CREATE_RETRY_MAX_ATTEMPTS:-4}"
    export SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS="${SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS:-0.5}"
    export SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS="${SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS:-8}"
}

ensure_port_available() {
    local port="$1"
    local label="$2"

    if lsof -Pi :"$port" -sTCP:LISTEN -t >/dev/null 2>&1; then
        echo "ERROR: $label port $port is already in use."
        exit 1
    fi
}

assert_frontend_build_exists() {
    local build_id_file="$FRONTEND_DIR/.next/BUILD_ID"
    local static_dir="$FRONTEND_DIR/.next/static"
    if [ ! -f "$build_id_file" ] || [ ! -d "$static_dir" ]; then
        echo "ERROR: frontend build artifacts are missing."
        echo "Expected:"
        echo "  $build_id_file"
        echo "  $static_dir"
        echo "Run: (cd $FRONTEND_DIR && npm run build)"
        exit 1
    fi
}

new_tmux_session() {
    local session_name="$1"
    local cwd="$2"
    local command="$3"
    local escaped

    escaped="$(printf '%q' "$command")"
    tmux new-session -d -s "$session_name" -c "$cwd" "bash -lc $escaped"
}

wait_for_session_running() {
    local session_name="$1"
    local attempts="${2:-10}"

    while [ "$attempts" -gt 0 ]; do
        if tmux_session_exists "$session_name"; then
            return 0
        fi
        attempts="$((attempts - 1))"
        sleep 1
    done

    echo "ERROR: session '$session_name' exited during startup."
    return 1
}

wait_for_http_ok() {
    local url="$1"
    local name="$2"
    local attempts="${3:-30}"

    if ! command -v curl >/dev/null 2>&1; then
        return 0
    fi

    while [ "$attempts" -gt 0 ]; do
        if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
            return 0
        fi
        attempts="$((attempts - 1))"
        sleep 1
    done

    echo "ERROR: $name did not become ready at $url"
    return 1
}

backend_shell_preamble() {
    cat <<EOF
set -euo pipefail
if [ -f "$CONDA_SH" ]; then
  source "$CONDA_SH"
  conda activate "$CONDA_ENV_NAME"
fi
export NODE_ENV=$(printf '%q' "$NODE_ENV")
export AGENT_BACKEND=$(printf '%q' "${AGENT_BACKEND:-agentscope}")
export DISABLE_LANGFUSE_PROMPT_FETCH=$(printf '%q' "${DISABLE_LANGFUSE_PROMPT_FETCH:-1}")
export CLAUDE_LOCAL_MODE=$(printf '%q' "${CLAUDE_LOCAL_MODE:-0}")
export BACKEND_PORT=$(printf '%q' "$BACKEND_PORT")
export BACKEND_RELOAD=$(printf '%q' "$BACKEND_RELOAD")
export FRONTEND_PORT=$(printf '%q' "$FRONTEND_PORT")
export PORT=$(printf '%q' "$PORT")
export REGULAR_EXECUTION_MODE=$(printf '%q' "$REGULAR_EXECUTION_MODE")
export SHADOW_CLONE_V2_EXECUTION_CHAIN=$(printf '%q' "$SHADOW_CLONE_V2_EXECUTION_CHAIN")
export SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS=$(printf '%q' "$SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS")
export SHADOW_CLONE_SUBAGENT_EXECUTION_MODE=$(printf '%q' "$SHADOW_CLONE_SUBAGENT_EXECUTION_MODE")
export DRAMATIQ_RUN_AGENT_QUEUE=$(printf '%q' "$DRAMATIQ_RUN_AGENT_QUEUE")
export DRAMATIQ_SANDBOX_CLEANUP_QUEUE=$(printf '%q' "$DRAMATIQ_SANDBOX_CLEANUP_QUEUE")
export SHADOW_CLONE_SUBAGENT_QUEUE=$(printf '%q' "$SHADOW_CLONE_SUBAGENT_QUEUE")
export REGULAR_SUPERVISOR_QUEUE=$(printf '%q' "$REGULAR_SUPERVISOR_QUEUE")
export DRAMATIQ_PROCESSES=$(printf '%q' "$DRAMATIQ_PROCESSES")
export DRAMATIQ_THREADS=$(printf '%q' "$DRAMATIQ_THREADS")
export REGULAR_SUPERVISOR_PROCESSES=$(printf '%q' "$REGULAR_SUPERVISOR_PROCESSES")
export REGULAR_SUPERVISOR_THREADS=$(printf '%q' "$REGULAR_SUPERVISOR_THREADS")
export REGULAR_SUPERVISOR_PERSISTENT=$(printf '%q' "$REGULAR_SUPERVISOR_PERSISTENT")
export REGULAR_SUPERVISOR_SHARD_PREFIX=$(printf '%q' "$REGULAR_SUPERVISOR_SHARD_PREFIX")
export REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS=$(printf '%q' "$REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS")
export AGENTSCOPE_SERVER_CONCURRENCY_BUDGET=$(printf '%q' "$AGENTSCOPE_SERVER_CONCURRENCY_BUDGET")
export AGENTSCOPE_SERVER_REGULAR_RUN_COST=$(printf '%q' "$AGENTSCOPE_SERVER_REGULAR_RUN_COST")
export AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST=$(printf '%q' "$AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST")
export AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET=$(printf '%q' "$AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET")
export AGENTSCOPE_SERVER_RUN_CAPACITY_TTL_SECONDS=$(printf '%q' "$AGENTSCOPE_SERVER_RUN_CAPACITY_TTL_SECONDS")
export REGULAR_QUEUE_MAX_DEPTH=$(printf '%q' "$REGULAR_QUEUE_MAX_DEPTH")
export REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH=$(printf '%q' "$REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH")
export SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL=$(printf '%q' "$SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL")
export SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX=$(printf '%q' "$SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX")
export SANDBOX_SHARED_LEASE_TTL_SECONDS=$(printf '%q' "$SANDBOX_SHARED_LEASE_TTL_SECONDS")
export SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL=$(printf '%q' "$SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL")
export SANDBOX_CREATE_QUEUE_TIMEOUT_SECONDS=$(printf '%q' "$SANDBOX_CREATE_QUEUE_TIMEOUT_SECONDS")
export SANDBOX_CREATE_LEASE_TTL_SECONDS=$(printf '%q' "$SANDBOX_CREATE_LEASE_TTL_SECONDS")
export SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS=$(printf '%q' "$SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS")
export SANDBOX_CREATE_RETRY_MAX_ATTEMPTS=$(printf '%q' "$SANDBOX_CREATE_RETRY_MAX_ATTEMPTS")
export SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS=$(printf '%q' "$SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS")
export SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS=$(printf '%q' "$SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS")
EOF
}

main() {
    require_tmux
    require_port_tools
    require_curl

    mkdir -p "$LOG_DIR"

    if [ ! -d "$BACKEND_DIR" ] || [ ! -d "$FRONTEND_DIR" ]; then
        echo "ERROR: backend/frontend directories are missing."
        exit 1
    fi

    assert_frontend_build_exists
    load_backend_env_if_present
    set_wrapper_defaults

    ensure_no_matching_processes "regular_supervisor_background --queues ${REGULAR_SUPERVISOR_QUEUE}" "regular supervisor queue ${REGULAR_SUPERVISOR_QUEUE}"

    ensure_port_available "$BACKEND_PORT" "backend"
    ensure_port_available "$FRONTEND_PORT" "frontend"

    ensure_session_absent "$SESSION_BACKEND"
    ensure_session_absent "$SESSION_RUN_AGENT"
    ensure_session_absent "$SESSION_REGULAR_SUPERVISOR"
    ensure_session_absent "$SESSION_FRONTEND"

    local preamble
    preamble="$(backend_shell_preamble)"

    local backend_cmd
    backend_cmd="$preamble
exec python3 api.py >> \"$BACKEND_LOG\" 2>&1"
    new_tmux_session "$SESSION_BACKEND" "$BACKEND_DIR" "$backend_cmd"
    wait_for_session_running "$SESSION_BACKEND"
    wait_for_http_ok "http://127.0.0.1:${BACKEND_PORT}/api/health" "backend API" 40

    local run_agent_cmd
    run_agent_cmd="$preamble
if command -v dramatiq >/dev/null 2>&1; then
  exec dramatiq --processes \"$DRAMATIQ_PROCESSES\" --threads \"$DRAMATIQ_THREADS\" run_agent_background >> \"$RUN_AGENT_LOG\" 2>&1
else
  exec python3 -m dramatiq --processes \"$DRAMATIQ_PROCESSES\" --threads \"$DRAMATIQ_THREADS\" run_agent_background >> \"$RUN_AGENT_LOG\" 2>&1
fi"
    new_tmux_session "$SESSION_RUN_AGENT" "$BACKEND_DIR" "$run_agent_cmd"
    wait_for_session_running "$SESSION_RUN_AGENT"

    local regular_supervisor_cmd
    regular_supervisor_cmd="$preamble
if command -v dramatiq >/dev/null 2>&1; then
  exec dramatiq --processes \"$REGULAR_SUPERVISOR_PROCESSES\" --threads \"$REGULAR_SUPERVISOR_THREADS\" regular_supervisor_background --queues \"$REGULAR_SUPERVISOR_QUEUE\" >> \"$REGULAR_SUPERVISOR_LOG\" 2>&1
else
  exec python3 -m dramatiq --processes \"$REGULAR_SUPERVISOR_PROCESSES\" --threads \"$REGULAR_SUPERVISOR_THREADS\" regular_supervisor_background --queues \"$REGULAR_SUPERVISOR_QUEUE\" >> \"$REGULAR_SUPERVISOR_LOG\" 2>&1
fi"
    new_tmux_session "$SESSION_REGULAR_SUPERVISOR" "$BACKEND_DIR" "$regular_supervisor_cmd"
    wait_for_session_running "$SESSION_REGULAR_SUPERVISOR"

    local frontend_cmd
    frontend_cmd="set -euo pipefail
export PORT=\"$FRONTEND_PORT\"
export NODE_ENV=\"production\"
exec npm run start >> \"$FRONTEND_LOG\" 2>&1"
    new_tmux_session "$SESSION_FRONTEND" "$FRONTEND_DIR" "$frontend_cmd"
    wait_for_session_running "$SESSION_FRONTEND"
    wait_for_http_ok "http://127.0.0.1:${FRONTEND_PORT}" "frontend" 40

    echo "Started WilliamManus production sessions:"
    echo "  $SESSION_BACKEND"
    echo "  $SESSION_RUN_AGENT"
    echo "  $SESSION_REGULAR_SUPERVISOR"
    echo "  $SESSION_FRONTEND"
    echo ""
    echo "Backend:  http://127.0.0.1:${BACKEND_PORT}"
    echo "Frontend: http://127.0.0.1:${FRONTEND_PORT}"
    echo "Public ingress expectation: nginx :80 -> frontend :${FRONTEND_PORT}, /api -> backend :${BACKEND_PORT}"
    echo ""
    echo "Logs:"
    echo "  $BACKEND_LOG"
    echo "  $RUN_AGENT_LOG"
    echo "  $REGULAR_SUPERVISOR_LOG"
    echo "  $FRONTEND_LOG"
}

main "$@"
