#!/usr/bin/env bash

set -euo pipefail

SESSION_FRONTEND="wm-frontend"
SESSION_REGULAR_SUPERVISOR="wm-regular-supervisor"
SESSION_RUN_AGENT="wm-dramatiq"
SESSION_BACKEND="wm-backend"
REGULAR_SUPERVISOR_QUEUE="${REGULAR_SUPERVISOR_QUEUE:-regular_supervisor_prod}"

require_tmux() {
    if ! command -v tmux >/dev/null 2>&1; then
        echo "ERROR: tmux is required but not installed."
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

terminate_matching_processes() {
    local pattern="$1"
    local label="$2"
    local graceful_wait_seconds="${3:-5}"
    local elapsed=0
    local pids

    pids="$(list_matching_pids "$pattern")"
    if [ -z "$pids" ]; then
        echo "[skip] no orphan $label processes found."
        return 0
    fi

    echo "[cleanup] Found orphan $label process(es):"
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        ps -fp "$pid" || true
    done <<< "$pids"

    echo "[cleanup] Sending TERM to orphan $label process(es) ..."
    while IFS= read -r pid; do
        [ -n "$pid" ] || continue
        kill "$pid" 2>/dev/null || true
    done <<< "$pids"

    while [ "$elapsed" -lt "$graceful_wait_seconds" ]; do
        if [ -z "$(list_matching_pids "$pattern")" ]; then
            echo "[ok] orphan $label processes stopped."
            return 0
        fi
        sleep 1
        elapsed="$((elapsed + 1))"
    done

    pids="$(list_matching_pids "$pattern")"
    if [ -n "$pids" ]; then
        echo "[cleanup] Escalating to KILL for orphan $label process(es) ..."
        while IFS= read -r pid; do
            [ -n "$pid" ] || continue
            kill -9 "$pid" 2>/dev/null || true
        done <<< "$pids"
        sleep 1
    fi

    if [ -n "$(list_matching_pids "$pattern")" ]; then
        echo "[error] Failed to stop orphan $label processes."
        return 1
    fi

    echo "[ok] orphan $label processes killed."
    return 0
}

stop_one_session() {
    local session_name="$1"
    local graceful_wait_seconds="${2:-8}"
    local elapsed=0

    if ! tmux_session_exists "$session_name"; then
        echo "[skip] $session_name is not running."
        return 0
    fi

    echo "[stop] Sending Ctrl-C to $session_name ..."
    tmux send-keys -t "$session_name" C-c || true

    while [ "$elapsed" -lt "$graceful_wait_seconds" ]; do
        if ! tmux_session_exists "$session_name"; then
            echo "[ok] $session_name stopped gracefully."
            return 0
        fi
        sleep 1
        elapsed="$((elapsed + 1))"
    done

    echo "[force] $session_name still running; killing tmux session ..."
    tmux kill-session -t "$session_name" || true
    sleep 1

    if tmux_session_exists "$session_name"; then
        echo "[error] Failed to stop $session_name"
        return 1
    fi

    echo "[ok] $session_name killed."
    return 0
}

main() {
    require_tmux

    local failed=0

    stop_one_session "$SESSION_FRONTEND" || failed=1
    stop_one_session "$SESSION_REGULAR_SUPERVISOR" || failed=1
    stop_one_session "$SESSION_RUN_AGENT" || failed=1
    stop_one_session "$SESSION_BACKEND" || failed=1
    terminate_matching_processes "regular_supervisor_background --queues ${REGULAR_SUPERVISOR_QUEUE}" "production regular supervisor queue ${REGULAR_SUPERVISOR_QUEUE}" || failed=1

    if [ "$failed" -ne 0 ]; then
        echo "WilliamManus production stop completed with errors."
        exit 1
    fi

    echo "WilliamManus production sessions stopped."
}

main "$@"
