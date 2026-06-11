#!/usr/bin/env python3
"""
Repair ADK `events.actions` values to be compatible with google-adk>=1.12.0.

Problem:
  ADK expects `actions` to unpickle into a Pydantic EventActions model.
  Older/custom code may have stored:
    - a pickled plain dict
    - empty/invalid bytes (causing EOFError / "Ran out of input")

This script rewrites actions to `pickle.dumps(EventActions(...))` where needed.

Usage examples:
  # Repair everything (can be slow if events table is large)
  python backend/backend/scripts/06_repair_adk_event_actions.py

  # Repair a single session only (recommended first)
  python backend/backend/scripts/06_repair_adk_event_actions.py --session-id <SESSION_ID>
"""

import argparse
import asyncio
import pickle
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from google.adk.events.event_actions import EventActions  # type: ignore

from services.postgresql import DBConnection
from utils.logger import logger


def _coerce_actions(actions_bytes: bytes | None) -> EventActions | None:
    """
    Return a repaired EventActions instance if the stored value is incompatible.
    Return None if no repair is needed.
    """
    if actions_bytes is None:
        return EventActions()

    try:
        obj = pickle.loads(actions_bytes)

        # Handle double-pickled bytes (rare, but possible if someone pickled bytes).
        for _ in range(2):
            if isinstance(obj, (bytes, bytearray)):
                obj = pickle.loads(obj)
            else:
                break

        if isinstance(obj, EventActions):
            return None
        if isinstance(obj, dict):
            return EventActions.model_validate(obj)

        # Unknown type -> replace with empty defaults.
        return EventActions()
    except Exception:
        # Invalid pickle -> replace with empty defaults.
        return EventActions()


async def _run(args: argparse.Namespace) -> int:
    db = DBConnection()
    await db.initialize()
    client = await db.client

    where = []
    params: list[object] = []
    if args.session_id:
        params.append(args.session_id)
        where.append(f"session_id = ${len(params)}")
    if args.app_name:
        params.append(args.app_name)
        where.append(f"app_name = ${len(params)}")
    if args.user_id:
        params.append(args.user_id)
        where.append(f"user_id = ${len(params)}")

    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    limit_sql = f"LIMIT {int(args.limit)}" if args.limit else ""

    select_sql = f"""
        SELECT id, app_name, user_id, session_id, actions
        FROM events
        {where_sql}
        ORDER BY timestamp DESC
        {limit_sql}
    """

    scanned = 0
    repaired = 0

    async with client.pool.acquire() as conn:
        rows = await conn.fetch(select_sql, *params)
        for row in rows:
            scanned += 1
            fixed = _coerce_actions(row["actions"])
            if fixed is None:
                continue

            repaired += 1
            new_bytes = pickle.dumps(fixed)
            await conn.execute(
                """
                UPDATE events
                SET actions = $1
                WHERE id = $2 AND app_name = $3 AND user_id = $4 AND session_id = $5
                """,
                new_bytes,
                row["id"],
                row["app_name"],
                row["user_id"],
                row["session_id"],
            )

    logger.info(f"Scanned {scanned} events; repaired {repaired} actions payloads.")
    await DBConnection.disconnect()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id", dest="session_id", default=None)
    parser.add_argument("--app-name", dest="app_name", default=None)
    parser.add_argument("--user-id", dest="user_id", default=None)
    parser.add_argument("--limit", dest="limit", default=None)
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

