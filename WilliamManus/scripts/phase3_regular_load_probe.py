from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import ceil
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from typing import Any


BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from services import regular_supervisor_runtime


@dataclass(frozen=True)
class _Filter:
    kind: str
    field: str
    value: object


class _ProbeTableQuery:
    def __init__(self, tables: dict[str, list[dict[str, Any]]], table_name: str):
        self._tables = tables
        self._table_name = table_name
        self._action = "select"
        self._filters: list[_Filter] = []
        self._insert_payload: dict[str, Any] | None = None
        self._update_payload: dict[str, Any] | None = None
        self._order_fields: list[tuple[str, bool]] = []
        self._limit: int | None = None

    def select(self, *_args, **_kwargs):
        self._action = "select"
        return self

    def insert(self, payload: dict[str, Any]):
        self._action = "insert"
        self._insert_payload = dict(payload)
        return self

    def update(self, payload: dict[str, Any]):
        self._action = "update"
        self._update_payload = dict(payload)
        return self

    def eq(self, field: str, value: object):
        self._filters.append(_Filter("eq", field, value))
        return self

    def in_(self, field: str, values: list[object]):
        self._filters.append(_Filter("in", field, tuple(values)))
        return self

    def is_(self, field: str, value: object):
        self._filters.append(_Filter("is", field, value))
        return self

    def lte(self, field: str, value: object):
        self._filters.append(_Filter("lte", field, value))
        return self

    def order(self, field: str, *, desc: bool = False):
        self._order_fields.append((field, bool(desc)))
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    async def execute(self):
        rows = self._tables.setdefault(self._table_name, [])

        if self._action == "insert":
            assert self._insert_payload is not None
            inserted = dict(self._insert_payload)
            rows.append(inserted)
            return SimpleNamespace(data=[dict(inserted)])

        def _matches(row: dict[str, Any]) -> bool:
            for current_filter in self._filters:
                row_value = row.get(current_filter.field)
                if current_filter.kind == "eq":
                    if row_value != current_filter.value:
                        return False
                    continue
                if current_filter.kind == "in":
                    if row_value not in current_filter.value:
                        return False
                    continue
                if current_filter.kind == "is":
                    if current_filter.value is None:
                        if row_value is not None:
                            return False
                        continue
                    if row_value is not current_filter.value:
                        return False
                    continue
                if current_filter.kind == "lte":
                    if row_value is None or row_value > current_filter.value:
                        return False
                    continue
                return False
            return True

        matched_rows = [row for row in rows if _matches(row)]
        if self._action == "update":
            payload = self._update_payload or {}
            updated_rows: list[dict[str, Any]] = []
            for row in matched_rows:
                row.update(payload)
                updated_rows.append(dict(row))
            return SimpleNamespace(data=updated_rows)

        selected_rows = [dict(row) for row in matched_rows]
        for field, desc in reversed(self._order_fields):
            selected_rows.sort(
                key=lambda row: row.get(field) or "",
                reverse=desc,
            )
        if self._limit is not None:
            selected_rows = selected_rows[: self._limit]
        return SimpleNamespace(data=selected_rows)


class _ProbeClient:
    def __init__(
        self,
        *,
        attempt_rows: list[dict[str, Any]],
        agent_run_rows: list[dict[str, Any]],
    ) -> None:
        self.tables = {
            "regular_run_attempts": [dict(row) for row in attempt_rows],
            "agent_runs": [dict(row) for row in agent_run_rows],
        }

    def table(self, table_name: str) -> _ProbeTableQuery:
        return _ProbeTableQuery(self.tables, table_name)

    def mark_parent_run_completed(self, agent_run_id: str) -> None:
        for row in self.tables["agent_runs"]:
            if str(row.get("agent_run_id") or "").strip() != agent_run_id:
                continue
            row["status"] = "completed"
            row["error"] = None
            return


class _ProbePubSub:
    async def psubscribe(self, *_args, **_kwargs) -> None:
        return None

    async def get_message(self, *_args, **_kwargs) -> None:
        await asyncio.sleep(0)
        return None

    async def punsubscribe(self, *_args, **_kwargs) -> None:
        return None

    async def unsubscribe(self, *_args, **_kwargs) -> None:
        return None

    async def aclose(self) -> None:
        return None


def _build_probe_rows(
    *,
    target_active_runs: int,
    queued_at: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    attempt_rows: list[dict[str, Any]] = []
    agent_run_rows: list[dict[str, Any]] = []
    for index in range(1, target_active_runs + 1):
        agent_run_id = f"run-{index}"
        attempt_id = f"attempt-{index}"
        attempt_rows.append(
            {
                "attempt_id": attempt_id,
                "agent_run_id": agent_run_id,
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "queued",
                "queued_at": queued_at,
                "metadata": json.dumps({}),
            }
        )
        agent_run_rows.append(
            {
                "agent_run_id": agent_run_id,
                "status": "queued",
                "error": None,
                "active_attempt_id": None,
                "current_execution_epoch": 1,
                "stream_source_epoch": 1,
                "active_supervisor_id": None,
                "regular_execution_backend": None,
            }
        )
    return attempt_rows, agent_run_rows


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = max(0, ceil(len(sorted_values) * 0.95) - 1)
    return round(sorted_values[index], 3)


async def run_probe(
    *,
    target_active_runs: int,
    active_duration_seconds: float = 0.05,
    max_concurrency: int | None = None,
) -> dict[str, Any]:
    queued_at = datetime.now(timezone.utc)
    attempt_rows, agent_run_rows = _build_probe_rows(
        target_active_runs=target_active_runs,
        queued_at=queued_at,
    )
    client = _ProbeClient(
        attempt_rows=attempt_rows,
        agent_run_rows=agent_run_rows,
    )
    resolved_max_concurrency = max_concurrency or target_active_runs
    metric_events: list[dict[str, Any]] = []
    target_reached_at: float | None = None
    first_complete_at: float | None = None
    start_time = time.monotonic()

    async def _metrics_sink(snapshot: dict[str, Any]) -> None:
        nonlocal target_reached_at
        nonlocal first_complete_at
        event_time = time.monotonic()
        metric_events.append({**snapshot, "event_time": event_time})
        if target_reached_at is None and int(snapshot.get("active_slots") or 0) >= target_active_runs:
            target_reached_at = event_time
        if first_complete_at is None and snapshot.get("event") == "complete":
            first_complete_at = event_time

    async def _executor(
        attempt_row: dict[str, Any],
        _owner_token: str,
        *,
        control_plane_slot: Any | None = None,
    ) -> None:
        del control_plane_slot
        await asyncio.sleep(active_duration_seconds)
        client.mark_parent_run_completed(str(attempt_row.get("agent_run_id") or "").strip())

    original_control_plane_class = regular_supervisor_runtime.RegularSupervisorControlPlane
    original_heartbeat = regular_supervisor_runtime._get_attempt_heartbeat_interval_seconds
    original_idle_poll = regular_supervisor_runtime._get_supervisor_idle_poll_interval_seconds
    original_idle_exit = regular_supervisor_runtime._get_supervisor_idle_exit_seconds
    original_persistent = regular_supervisor_runtime._is_persistent_supervisor_enabled
    regular_supervisor_runtime.RegularSupervisorControlPlane = lambda supervisor_id: original_control_plane_class(
        supervisor_id=supervisor_id,
        pubsub_factory=lambda: asyncio.sleep(0, result=_ProbePubSub()),
    )
    regular_supervisor_runtime._get_attempt_heartbeat_interval_seconds = (
        lambda: max(0.01, active_duration_seconds / 2)
    )
    regular_supervisor_runtime._get_supervisor_idle_poll_interval_seconds = lambda: 0.01
    regular_supervisor_runtime._get_supervisor_idle_exit_seconds = lambda: 0.05
    regular_supervisor_runtime._is_persistent_supervisor_enabled = lambda: False
    try:
        completed_attempt_ids = await regular_supervisor_runtime.run_supervisor_loop(
            client=client,
            owner_token="phase3-probe:slot-1",
            supervisor_id="phase3-probe",
            executor=_executor,
            max_concurrency=resolved_max_concurrency,
            metrics_sink=_metrics_sink,
        )
    finally:
        regular_supervisor_runtime.RegularSupervisorControlPlane = original_control_plane_class
        regular_supervisor_runtime._get_attempt_heartbeat_interval_seconds = original_heartbeat
        regular_supervisor_runtime._get_supervisor_idle_poll_interval_seconds = original_idle_poll
        regular_supervisor_runtime._get_supervisor_idle_exit_seconds = original_idle_exit
        regular_supervisor_runtime._is_persistent_supervisor_enabled = original_persistent

    finished_at = time.monotonic()
    finalized_attempts = client.tables["regular_run_attempts"]
    completed_count = sum(
        1
        for row in finalized_attempts
        if str(row.get("status") or "").strip().lower() == "completed"
    )
    queue_to_claim_ms = [
        (row["claimed_at"] - row["queued_at"]).total_seconds() * 1000
        for row in finalized_attempts
        if isinstance(row.get("claimed_at"), datetime)
        and isinstance(row.get("queued_at"), datetime)
    ]
    peak_active_slots = max(
        [int(event.get("peak_active_slots") or 0) for event in metric_events] or [0]
    )
    time_to_target_active_ms = (
        round((target_reached_at - start_time) * 1000, 3)
        if target_reached_at is not None
        else None
    )
    plateau_duration_ms = (
        round((first_complete_at - target_reached_at) * 1000, 3)
        if target_reached_at is not None and first_complete_at is not None
        else None
    )
    drain_convergence_ms = round((finished_at - start_time) * 1000, 3)
    return {
        "target_active_runs": target_active_runs,
        "max_concurrency": resolved_max_concurrency,
        "active_duration_seconds": active_duration_seconds,
        "completed_runs": completed_count,
        "completed_attempt_ids": len(completed_attempt_ids),
        "completion_ratio": round(completed_count / max(1, target_active_runs), 3),
        "peak_active_slots": peak_active_slots,
        "time_to_target_active_ms": time_to_target_active_ms,
        "plateau_duration_ms": plateau_duration_ms,
        "drain_convergence_ms": drain_convergence_ms,
        "p95_start_delay_ms": _p95(queue_to_claim_ms),
        "metric_events": len(metric_events),
    }


async def run_probe_matrix(
    *,
    targets: tuple[int, ...] = (25, 50, 100),
    active_duration_seconds: float = 0.05,
    max_concurrency: int | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in targets:
        results.append(
            await run_probe(
                target_active_runs=target,
                active_duration_seconds=active_duration_seconds,
                max_concurrency=max_concurrency,
            )
        )
    return results


def _parse_targets(raw_targets: str) -> tuple[int, ...]:
    parsed_targets = tuple(
        max(1, int(segment.strip()))
        for segment in str(raw_targets or "").split(",")
        if segment.strip()
    )
    return parsed_targets or (25, 50, 100)


async def _run_from_args(args: argparse.Namespace) -> int:
    results = await run_probe_matrix(
        targets=_parse_targets(args.targets),
        active_duration_seconds=args.active_duration_seconds,
        max_concurrency=args.max_concurrency,
    )
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Control-plane-focused Phase 3 regular supervisor load probe",
    )
    parser.add_argument("--targets", default="25,50,100")
    parser.add_argument("--active-duration-seconds", type=float, default=0.05)
    parser.add_argument("--max-concurrency", type=int, default=None)
    args = parser.parse_args()
    return asyncio.run(_run_from_args(args))


if __name__ == "__main__":
    raise SystemExit(main())
