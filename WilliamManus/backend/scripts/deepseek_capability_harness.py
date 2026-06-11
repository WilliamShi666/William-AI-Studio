from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from eval.deepseek_capability_harness import (
    RunObservation,
    ToolCallRecord,
    build_capability_report,
    default_capability_tasks,
    real_artifact_capability_tasks,
)
from eval.deepseek_capability_api_runner import DeepSeekCapabilityApiRunner

try:
    import httpx
except Exception:  # pragma: no cover - handled at runtime for minimal installs
    httpx = None


def _task_to_json(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "category": task.category,
        "prompt": task.prompt,
        "expected_tools": list(task.expected_tools),
        "success_criteria": [dict(item) for item in task.success_criteria],
        "model_family": task.model_family,
        "requires_artifact_verification": task.requires_artifact_verification,
        "max_duration_seconds": task.max_duration_seconds,
    }


def _load_observations(path: Path) -> tuple[RunObservation, ...]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("observations file must contain a JSON list")

    observations: list[RunObservation] = []
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("each observation must be a JSON object")
        tool_calls = tuple(
            ToolCallRecord(
                name=str(tool_call.get("name") or ""),
                arguments=dict(tool_call.get("arguments") or {}),
            )
            for tool_call in item.get("tool_calls", [])
            if isinstance(tool_call, dict)
        )
        observations.append(
            RunObservation(
                task_id=str(item.get("task_id") or ""),
                attempt=int(item.get("attempt") or 1),
                status=str(item.get("status") or ""),
                duration_seconds=item.get("duration_seconds"),
                artifact_verified=item.get("artifact_verified"),
                tool_calls=tool_calls,
                error=item.get("error"),
            )
        )
    return tuple(observations)


def _tasks_for_set(task_set: str | None) -> tuple[Any, ...]:
    normalized = str(task_set or "default").strip().lower()
    if normalized in {"default", "smoke", "regression"}:
        return default_capability_tasks()
    if normalized in {"real-artifacts", "real_artifacts", "real"}:
        return real_artifact_capability_tasks()
    raise ValueError(f"Unknown task set: {task_set}")


def _select_tasks(task_ids: str | None, *, task_set: str | None = None) -> tuple[Any, ...]:
    tasks = _tasks_for_set(task_set)
    if not task_ids:
        return tasks
    requested = {task_id.strip() for task_id in task_ids.split(",") if task_id.strip()}
    selected = tuple(task for task in tasks if task.task_id in requested)
    missing = sorted(requested - {task.task_id for task in selected})
    if missing:
        raise ValueError(f"Unknown task id(s): {', '.join(missing)}")
    return selected


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="DeepSeek/Roys Alpha capability harness"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser(
        "list-tasks", help="Print the default capability task set as JSON"
    ).add_argument(
        "--task-set",
        choices=("default", "real-artifacts"),
        default="default",
        help="Task set to print",
    )

    grade_parser = subparsers.add_parser(
        "grade-observations",
        help="Grade a JSON observation file and print a report",
    )
    grade_parser.add_argument(
        "--observations", required=True, help="Path to observations JSON"
    )
    grade_parser.add_argument(
        "--model-name",
        default="deepseek-v4-pro-high",
        help="Model name to include in the report",
    )
    grade_parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO timestamp to include in the report; defaults to current UTC time",
    )
    grade_parser.add_argument(
        "--task-set",
        choices=("default", "real-artifacts"),
        default="default",
        help="Task set to grade against",
    )

    run_parser = subparsers.add_parser(
        "run",
        help="Run selected capability tasks against a live Roys Alpha API",
    )
    run_parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8002/api",
        help="Backend API base URL",
    )
    run_parser.add_argument(
        "--auth-token",
        default=None,
        help="Bearer token. Defaults to DEEPSEEK_HARNESS_AUTH_TOKEN env var.",
    )
    run_parser.add_argument(
        "--model-name",
        default="deepseek-v4-pro-high",
        help="Model name to use for live runs",
    )
    run_parser.add_argument(
        "--tasks",
        default=None,
        help="Comma-separated task ids. Defaults to all tasks.",
    )
    run_parser.add_argument(
        "--task-set",
        choices=("default", "real-artifacts"),
        default="default",
        help="Task set to run",
    )
    run_parser.add_argument("--attempts", type=int, default=1)
    run_parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    run_parser.add_argument("--max-poll-attempts", type=int, default=72)
    run_parser.add_argument(
        "--generated-at",
        default=None,
        help="ISO timestamp to include in the report; defaults to current UTC time",
    )
    return parser


def _print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


async def _run_live(args: Any) -> dict[str, Any]:
    if httpx is None:
        raise RuntimeError("httpx is required for live harness runs")

    auth_token = str(args.auth_token or os.getenv("DEEPSEEK_HARNESS_AUTH_TOKEN") or "")
    if not auth_token:
        raise ValueError(
            "Live run requires --auth-token or DEEPSEEK_HARNESS_AUTH_TOKEN"
        )

    async with httpx.AsyncClient(timeout=120) as client:
        runner = DeepSeekCapabilityApiRunner(
            client=client,
            base_url=args.base_url,
            auth_token=auth_token,
            model_name=args.model_name,
            poll_interval_seconds=args.poll_interval_seconds,
            max_poll_attempts=args.max_poll_attempts,
        )
        return await runner.run_tasks(
            tasks=_select_tasks(args.tasks, task_set=args.task_set),
            attempts=args.attempts,
            generated_at=(args.generated_at or datetime.now(timezone.utc).isoformat()),
        )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    tasks = default_capability_tasks()

    if args.command == "list-tasks":
        tasks = _tasks_for_set(args.task_set)
        _print_json(
            {
                "schema_version": 1,
                "tasks": [_task_to_json(task) for task in tasks],
            }
        )
        return 0

    if args.command == "grade-observations":
        generated_at = args.generated_at or datetime.now(timezone.utc).isoformat()
        report = build_capability_report(
            tasks=_tasks_for_set(args.task_set),
            observations=_load_observations(Path(args.observations)),
            model_name=args.model_name,
            generated_at=generated_at,
        )
        _print_json(report)
        return 0

    if args.command == "run":
        _print_json(asyncio.run(_run_live(args)))
        return 0

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
