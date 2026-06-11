#!/usr/bin/env python3
"""
Incremental migration to align ADK `events` table with google-adk>=1.12.0.

Older installs may be missing optional columns introduced by ADK:
  - custom_metadata
  - usage_metadata
  - citation_metadata
  - input_transcription
  - output_transcription

This script safely adds them with ALTER TABLE ... IF NOT EXISTS.
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.postgresql import DBConnection
from utils.logger import logger


async def run_migration() -> bool:
    db = None
    try:
        db = DBConnection()
        await db.initialize()
        client = await db.client

        migration_file = project_root / "migrations" / "adk_events_add_missing_columns.sql"
        with open(migration_file, "r", encoding="utf-8") as f:
            sql = f.read()

        async with client.pool.acquire() as conn:
            await conn.execute(sql)

        logger.info("ADK events columns migration applied successfully.")
        return True
    except Exception as e:
        logger.error(f"Failed to apply ADK events columns migration: {e}", exc_info=True)
        return False
    finally:
        if db:
            await DBConnection.disconnect()


def main():
    print("ADK events table incremental migration")
    print("=" * 50)
    ok = asyncio.run(run_migration())
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()

