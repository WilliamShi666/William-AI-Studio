from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Optional

import asyncpg

_POOL: Optional[asyncpg.Pool] = None
_DATABASE_URL: Optional[str] = None


def _parse_env_file(path: Path) -> dict[str, str]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}

    env: dict[str, str] = {}
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip("\"").strip("'")
    return env


def _resolve_database_url() -> str:
    global _DATABASE_URL
    if _DATABASE_URL:
        return _DATABASE_URL

    env_value = os.getenv("DATABASE_URL")
    if env_value:
        _DATABASE_URL = env_value
        return env_value

    repo_root = Path(__file__).resolve().parents[3]
    candidates = [
        repo_root / "multimodalrag" / "backend" / ".env",
        repo_root / "WilliamManus" / "backend" / ".env",
    ]

    for path in candidates:
        env_map = _parse_env_file(path)
        value = env_map.get("DATABASE_URL")
        if value:
            os.environ.setdefault("DATABASE_URL", value)
            _DATABASE_URL = value
            return value

    raise RuntimeError("DATABASE_URL is not configured")


async def get_pool() -> asyncpg.Pool:
    global _POOL
    if _POOL is None:
        database_url = _resolve_database_url()
        _POOL = await asyncpg.create_pool(
            database_url,
            min_size=10,
            max_size=300,
            command_timeout=60,
        )
    return _POOL


async def purge_old_sessions(retention_days: int) -> None:
    if retention_days <= 0:
        return

    pool = await get_pool()
    await pool.execute(
        """
        delete from qa_sessions
        where updated_at < (now() - ($1::int * interval '1 day'))
        """,
        retention_days,
    )


async def create_session(
    account_id: str,
    title: Optional[str],
    model_name: Optional[str],
) -> dict[str, Any]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        insert into qa_sessions (account_id, title, model_name)
        values ($1, $2, $3)
        returning session_id, account_id, title, model_name, created_at, updated_at
        """,
        account_id,
        title,
        model_name,
    )
    return dict(row) if row else {}


async def list_sessions(account_id: str, limit: int = 50) -> list[dict[str, Any]]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        select session_id, account_id, title, model_name, created_at, updated_at
        from qa_sessions
        where account_id = $1
        order by updated_at desc
        limit $2
        """,
        account_id,
        limit,
    )
    return [dict(row) for row in rows]


async def get_session(account_id: str, session_id: str) -> Optional[dict[str, Any]]:
    pool = await get_pool()
    row = await pool.fetchrow(
        """
        select session_id, account_id, title, model_name, created_at, updated_at
        from qa_sessions
        where session_id = $1 and account_id = $2
        """,
        session_id,
        account_id,
    )
    return dict(row) if row else None


async def get_messages(session_id: str) -> list[dict[str, Any]]:
    pool = await get_pool()
    rows = await pool.fetch(
        """
        select message_id, session_id, role, content, images, files, reasoning_content, metadata, created_at
        from qa_messages
        where session_id = $1
        order by created_at asc
        """,
        session_id,
    )
    return [dict(row) for row in rows]


async def insert_message(
    session_id: str,
    role: str,
    content: str,
    images: Optional[list[str]] = None,
    files: Optional[list[dict[str, Any]]] = None,
    reasoning_content: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    pool = await get_pool()
    images_json = json.dumps(images) if images is not None else None
    files_json = json.dumps(files) if files is not None else None
    metadata_json = json.dumps(metadata) if metadata is not None else None
    row = await pool.fetchrow(
        """
        insert into qa_messages (session_id, role, content, images, files, reasoning_content, metadata)
        values ($1, $2, $3, $4::jsonb, $5::jsonb, $6, $7::jsonb)
        returning message_id, session_id, role, content, images, files, reasoning_content, metadata, created_at
        """,
        session_id,
        role,
        content,
        images_json,
        files_json,
        reasoning_content,
        metadata_json,
    )
    return dict(row) if row else {}


async def touch_session(session_id: str, title: Optional[str] = None, model_name: Optional[str] = None) -> None:
    pool = await get_pool()
    if title is not None or model_name is not None:
        await pool.execute(
            """
            update qa_sessions
            set updated_at = now(),
                title = coalesce($2, title),
                model_name = coalesce($3, model_name)
            where session_id = $1
            """,
            session_id,
            title,
            model_name,
        )
        return

    await pool.execute(
        """
        update qa_sessions
        set updated_at = now()
        where session_id = $1
        """,
        session_id,
    )


async def delete_session(account_id: str, session_id: str) -> bool:
    pool = await get_pool()
    result = await pool.execute(
        """
        delete from qa_sessions
        where session_id = $1 and account_id = $2
        """,
        session_id,
        account_id,
    )
    try:
        _, count = result.split(" ")
        return int(count) > 0
    except ValueError:
        return False
