from __future__ import annotations

import asyncio
import inspect
import os
import time
from typing import Any

from services import regular_run_attempts
from services.regular_supervisor_control_plane import RegularSupervisorControlPlane
from utils.logger import logger

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


class RegularSupervisorLostError(RuntimeError):
    """Raised when a claimed attempt loses supervisor ownership and should requeue."""


def _read_positive_float_env(name: str, default: float) -> float:
    raw_value = str(os.getenv(name, str(default))).strip()
    try:
        parsed_value = float(raw_value)
    except (TypeError, ValueError):
        return default
    return max(0.01, parsed_value)


def _get_attempt_heartbeat_interval_seconds() -> float:
    return _read_positive_float_env(
        "REGULAR_SUPERVISOR_ATTEMPT_HEARTBEAT_INTERVAL_SECONDS",
        5.0,
    )


def _get_supervisor_idle_poll_interval_seconds() -> float:
    return _read_positive_float_env(
        "REGULAR_SUPERVISOR_IDLE_POLL_INTERVAL_SECONDS",
        1.0,
    )


def _get_supervisor_idle_exit_seconds() -> float:
    return _read_positive_float_env(
        "REGULAR_SUPERVISOR_IDLE_EXIT_SECONDS",
        5.0,
    )


def _is_persistent_supervisor_enabled() -> bool:
    return (
        str(os.getenv("REGULAR_SUPERVISOR_PERSISTENT", "false")).strip().lower()
        in _TRUE_ENV_VALUES
    )


async def run_supervisor_loop(
    *,
    client: Any,
    owner_token: str,
    supervisor_id: str,
    executor: Any,
    max_concurrency: int,
    metrics_sink: Any | None = None,
) -> list[str]:
    normalized_owner_token = str(owner_token or "").strip()
    normalized_supervisor_id = str(supervisor_id or "").strip()
    concurrency_limit = int(max_concurrency)
    if not normalized_owner_token:
        raise ValueError("owner_token is required")
    if not normalized_supervisor_id:
        raise ValueError("supervisor_id is required")
    if concurrency_limit < 1:
        raise ValueError("max_concurrency must be positive")
    claim_backend = (
        "server_cursor"
        if getattr(client, "pool", None) is not None
        and hasattr(getattr(client, "pool", None), "acquire")
        else "legacy_scan"
    )

    completed_attempt_ids: list[str] = []
    in_flight: dict[asyncio.Task, dict[str, str]] = {}
    idle_exit_after_seconds = _get_supervisor_idle_exit_seconds()
    idle_poll_interval_seconds = _get_supervisor_idle_poll_interval_seconds()
    refresh_tick_interval_seconds = _get_attempt_heartbeat_interval_seconds()
    idle_deadline = time.monotonic() + idle_exit_after_seconds
    persistent_supervisor_enabled = _is_persistent_supervisor_enabled()
    last_refresh_tick_at = time.monotonic() - refresh_tick_interval_seconds
    peak_active_slots = 0
    claim_success_count = 0
    claim_empty_count = 0
    claim_error_count = 0
    reconcile_pass_count = 0
    reconcile_recovered_count = 0
    refresh_tick_count = 0
    control_plane = RegularSupervisorControlPlane(
        supervisor_id=normalized_supervisor_id,
    )
    control_plane_listener_task = asyncio.create_task(control_plane.run_stop_listener())

    try:
        executor_signature = inspect.signature(executor)
    except (TypeError, ValueError):
        executor_accepts_control_plane_slot = True
    else:
        executor_accepts_control_plane_slot = (
            "control_plane_slot" in executor_signature.parameters
            or any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in executor_signature.parameters.values()
            )
        )

    async def _execute_with_control_plane(
        attempt_row: dict[str, Any],
        owner: str,
        *,
        control_plane_slot: Any,
    ) -> None:
        if executor_accepts_control_plane_slot:
            await executor(
                dict(attempt_row),
                owner,
                control_plane_slot=control_plane_slot,
            )
            return
        await executor(dict(attempt_row), owner)

    async def _refresh_active_slot(slot: Any) -> None:
        refreshed_attempt = await regular_run_attempts.refresh_attempt_heartbeat(
            client,
            attempt_id=str(slot.attempt_id or "").strip(),
            owner_token=str(slot.owner_token or "").strip(),
        )
        if refreshed_attempt is None:
            return
        if slot.refresh_callback is None:
            return
        await slot.refresh_callback(slot)

    async def _emit_metrics(event: str, **extra: Any) -> None:
        if metrics_sink is None:
            return
        snapshot = {
            "event": event,
            "supervisor_id": normalized_supervisor_id,
            "active_slots": len(in_flight),
            "peak_active_slots": peak_active_slots,
            "completed_attempts": len(completed_attempt_ids),
            "claim_success_count": claim_success_count,
            "claim_empty_count": claim_empty_count,
            "claim_error_count": claim_error_count,
            "reconcile_pass_count": reconcile_pass_count,
            "reconcile_recovered_count": reconcile_recovered_count,
            "refresh_tick_count": refresh_tick_count,
        }
        snapshot.update(extra)
        sink_result = metrics_sink(snapshot)
        if inspect.isawaitable(sink_result):
            await sink_result

    async def _start_one() -> bool:
        nonlocal peak_active_slots
        nonlocal claim_success_count
        nonlocal claim_empty_count
        claim_started_at = time.monotonic()
        claimed_attempt = await regular_run_attempts.claim_next_attempt(
            client,
            supervisor_id=normalized_supervisor_id,
            owner_token=normalized_owner_token,
        )
        claim_latency_ms = round((time.monotonic() - claim_started_at) * 1000, 3)
        if claimed_attempt is None:
            claim_empty_count += 1
            await _emit_metrics(
                "claim",
                result="empty",
                claim_latency_ms=claim_latency_ms,
                claim_backend=claim_backend,
            )
            return False

        attempt_id = str(claimed_attempt.get("attempt_id") or "").strip()
        if not attempt_id:
            logger.warning(
                "Skipping claimed regular supervisor attempt without attempt_id supervisor_id=%s owner=%s",
                normalized_supervisor_id,
                normalized_owner_token,
            )
            return False
        claim_success_count += 1

        running_attempt = await regular_run_attempts.mark_attempt_running(
            client,
            attempt_id=attempt_id,
        )
        if running_attempt is None:
            logger.warning(
                "Failed to transition claimed regular supervisor attempt to running attempt_id=%s supervisor_id=%s owner=%s",
                attempt_id,
                normalized_supervisor_id,
                normalized_owner_token,
            )
            await _emit_metrics(
                "claim",
                result="running_transition_failed",
                attempt_id=attempt_id,
                claim_latency_ms=claim_latency_ms,
                claim_backend=claim_backend,
            )
            return False

        agent_run_id = str(running_attempt.get("agent_run_id") or "").strip()
        control_plane_slot = control_plane.register(
            agent_run_id=agent_run_id,
            attempt_id=attempt_id,
            execution_epoch=int(running_attempt.get("execution_epoch") or 0),
            owner_token=normalized_owner_token,
        )
        task = asyncio.create_task(
            _execute_with_control_plane(
                dict(running_attempt),
                normalized_owner_token,
                control_plane_slot=control_plane_slot,
            )
        )
        in_flight[task] = {
            "attempt_id": attempt_id,
            "agent_run_id": agent_run_id,
        }
        peak_active_slots = max(peak_active_slots, len(in_flight))
        await _emit_metrics(
            "claim",
            result="claimed",
            attempt_id=attempt_id,
            claim_latency_ms=claim_latency_ms,
            claim_backend=claim_backend,
        )
        return True

    try:
        while True:
            try:
                reconcile_started_at = time.monotonic()
                recovered_attempts = await regular_run_attempts.reconcile_expired_attempts(
                    client,
                    recovery_reason="supervisor_unresponsive",
                )
                reconcile_pass_count += 1
                reconcile_recovered_count += len(recovered_attempts)
                await _emit_metrics(
                    "reconcile",
                    result="ok",
                    reconcile_duration_ms=round(
                        (time.monotonic() - reconcile_started_at) * 1000,
                        3,
                    ),
                    reconcile_recovered_count=len(recovered_attempts),
                )
            except Exception as reconcile_error:
                logger.warning(
                    "Regular supervisor reconcile pass failed supervisor_id=%s owner=%s: %s",
                    normalized_supervisor_id,
                    normalized_owner_token,
                    reconcile_error,
                )
                await _emit_metrics(
                    "reconcile",
                    result="error",
                    reconcile_duration_ms=round(
                        (time.monotonic() - reconcile_started_at) * 1000,
                        3,
                    ),
                    error=str(reconcile_error),
                )
                await asyncio.sleep(idle_poll_interval_seconds)
                continue

            if in_flight and (
                time.monotonic() - last_refresh_tick_at >= refresh_tick_interval_seconds
            ):
                await control_plane.run_refresh_tick(_refresh_active_slot)
                last_refresh_tick_at = time.monotonic()
                refresh_tick_count += 1
                await _emit_metrics("refresh")

            while len(in_flight) < concurrency_limit:
                claim_started_at = time.monotonic()
                try:
                    started_attempt = await _start_one()
                except Exception as claim_error:
                    claim_error_count += 1
                    logger.warning(
                        "Regular supervisor claim/start failed supervisor_id=%s owner=%s: %s",
                        normalized_supervisor_id,
                        normalized_owner_token,
                        claim_error,
                    )
                    await _emit_metrics(
                        "claim",
                        result="error",
                        claim_backend=claim_backend,
                        claim_latency_ms=round(
                            (time.monotonic() - claim_started_at) * 1000,
                            3,
                        ),
                        error=str(claim_error),
                    )
                    await asyncio.sleep(idle_poll_interval_seconds)
                    started_attempt = False
                if not started_attempt:
                    break
                idle_deadline = time.monotonic() + idle_exit_after_seconds

            if not in_flight:
                try:
                    recoverable_attempts_pending = (
                        await regular_run_attempts.has_recoverable_attempts(client)
                    )
                except Exception as recoverable_error:
                    logger.warning(
                        "Regular supervisor recoverable-attempt probe failed supervisor_id=%s owner=%s: %s",
                        normalized_supervisor_id,
                        normalized_owner_token,
                        recoverable_error,
                    )
                    await asyncio.sleep(idle_poll_interval_seconds)
                    continue
                if recoverable_attempts_pending:
                    await asyncio.sleep(idle_poll_interval_seconds)
                    continue
                if persistent_supervisor_enabled:
                    await asyncio.sleep(idle_poll_interval_seconds)
                    continue
                if time.monotonic() >= idle_deadline:
                    break
                await asyncio.sleep(idle_poll_interval_seconds)
                continue

            done, _pending = await asyncio.wait(
                tuple(in_flight.keys()),
                timeout=min(idle_poll_interval_seconds, refresh_tick_interval_seconds),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                continue
            for task in done:
                task_metadata = in_flight.pop(task)
                attempt_id = task_metadata["attempt_id"]
                agent_run_id = task_metadata["agent_run_id"]
                try:
                    await asyncio.shield(task)
                    finalized_attempt = (
                        await regular_run_attempts.finalize_completed_attempt(
                            client,
                            attempt_id=attempt_id,
                        )
                    )
                    if finalized_attempt is None:
                        logger.warning(
                            "Regular supervisor completed attempt without terminalizing execution row attempt_id=%s supervisor_id=%s owner=%s",
                            attempt_id,
                            normalized_supervisor_id,
                            normalized_owner_token,
                        )
                except RegularSupervisorLostError as supervisor_lost_error:
                    logger.warning(
                        "Regular supervisor lost claimed attempt_id=%s supervisor_id=%s owner=%s: %s",
                        attempt_id,
                        normalized_supervisor_id,
                        normalized_owner_token,
                        supervisor_lost_error,
                    )
                    try:
                        await regular_run_attempts.reconcile_lost_attempt(
                            client,
                            attempt_id=attempt_id,
                            recovery_reason="supervisor_lost",
                        )
                    except Exception as reconcile_error:
                        logger.warning(
                            "Regular supervisor failed to reconcile lost attempt attempt_id=%s supervisor_id=%s owner=%s: %s",
                            attempt_id,
                            normalized_supervisor_id,
                            normalized_owner_token,
                            reconcile_error,
                        )
                except Exception as executor_error:
                    logger.warning(
                        "Regular supervisor executor failed for attempt_id=%s supervisor_id=%s owner=%s: %s",
                        attempt_id,
                        normalized_supervisor_id,
                        normalized_owner_token,
                        executor_error,
                    )
                finally:
                    control_plane.unregister(agent_run_id=agent_run_id)
                completed_attempt_ids.append(attempt_id)
                await _emit_metrics("complete", attempt_id=attempt_id)
    finally:
        control_plane_listener_task.cancel()
        await asyncio.gather(control_plane_listener_task, return_exceptions=True)
        await control_plane.aclose()

    return completed_attempt_ids
