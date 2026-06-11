from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from services.postgresql import DBConnection
from utils.logger import logger


SHADOW_CLONE_MODEL_CONFIG_KEY = "global"
_MISSING_TABLE_WARNING_EMITTED = False
_SHADOW_CLONE_OFFICIAL_KIMI_MODEL_ALIASES = {
    "kimi-k2.5": "kimi-k2.5",
    "moonshot/kimi-k2.5": "kimi-k2.5",
    "moonshotai/kimi-k2.5": "kimi-k2.5",
    "kimi/kimi-k2.5": "kimi-k2.5",
    "kimi-k2.6": "kimi-k2.6",
    "moonshot/kimi-k2.6": "kimi-k2.6",
    "moonshotai/kimi-k2.6": "kimi-k2.6",
    "kimi/kimi-k2.6": "kimi-k2.6",
}
_SHADOW_CLONE_OFFICIAL_DEEPSEEK_MODEL_IDS = frozenset(
    {
        "deepseek-v4-pro-high",
        "deepseek-v4-pro-max",
        "deepseek-v4-flash-high",
        "deepseek-v4-flash-max",
    }
)
_SHADOW_CLONE_UNSUPPORTED_QWEN_MARKERS = (
    "qwen3.5-plus",
    "qwen3.5-397b-a17b",
)
_CREATE_SHADOW_CLONE_MODEL_CONFIG_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS shadow_clone_model_config (
    config_key TEXT PRIMARY KEY CHECK (config_key = 'global'),
    main_model_name TEXT NOT NULL,
    subagent_model_name TEXT NOT NULL,
    updated_by UUID REFERENCES users(id) ON DELETE SET NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT timezone('utc', now())
)
"""


def normalize_shadow_clone_selected_model_name(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("is required")

    lowered = normalized.lower()
    kimi_model = _SHADOW_CLONE_OFFICIAL_KIMI_MODEL_ALIASES.get(lowered)
    if kimi_model:
        return kimi_model

    if lowered in _SHADOW_CLONE_OFFICIAL_DEEPSEEK_MODEL_IDS:
        return lowered

    if lowered.startswith("openrouter/"):
        if any(marker in lowered for marker in _SHADOW_CLONE_UNSUPPORTED_QWEN_MARKERS):
            raise ValueError(
                "must not target Qwen 3.5 Plus; choose a supported Shadow Clone model"
            )
        return normalized

    raise ValueError("must be an official Kimi/DeepSeek model or an openrouter/... model")


def _empty_shadow_clone_model_config() -> Dict[str, Optional[str]]:
    return {
        "main_model_name": None,
        "subagent_model_name": None,
        "updated_by": None,
        "updated_at": None,
    }


def _serialize_shadow_clone_model_config_row(row: Any) -> Dict[str, Optional[str]]:
    if not row:
        return _empty_shadow_clone_model_config()

    data = row if isinstance(row, dict) else dict(row)
    updated_at = data.get("updated_at")
    updated_by = data.get("updated_by")

    return {
        "main_model_name": data.get("main_model_name"),
        "subagent_model_name": data.get("subagent_model_name"),
        "updated_by": str(updated_by) if updated_by else None,
        "updated_at": (
            updated_at.isoformat()
            if isinstance(updated_at, datetime)
            else str(updated_at) if updated_at else None
        ),
    }


async def _get_client(client: Any = None):
    if client is not None:
        return client

    db = DBConnection()
    await db.initialize()
    return await db.client


def _is_missing_table_error(error: Exception) -> bool:
    message = str(error).lower()
    return "shadow_clone_model_config" in message and (
        "does not exist" in message or "undefined_table" in message
    )


def _log_missing_table_once(error: Exception) -> None:
    global _MISSING_TABLE_WARNING_EMITTED
    if _MISSING_TABLE_WARNING_EMITTED:
        return

    _MISSING_TABLE_WARNING_EMITTED = True
    logger.warning(
        "shadow_clone_model_config table is missing; falling back to legacy Shadow Clone defaults until migrations run: %s",
        error,
    )


async def _ensure_shadow_clone_model_config_table(resolved_client: Any) -> None:
    async with resolved_client.pool.acquire() as conn:
        await conn.execute(_CREATE_SHADOW_CLONE_MODEL_CONFIG_TABLE_SQL)


async def get_shadow_clone_model_config(client: Any = None) -> Dict[str, Optional[str]]:
    resolved_client = await _get_client(client)
    try:
        async with resolved_client.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT main_model_name, subagent_model_name, updated_by, updated_at
                FROM shadow_clone_model_config
                WHERE config_key = $1
                """,
                SHADOW_CLONE_MODEL_CONFIG_KEY,
            )
    except Exception as error:
        if _is_missing_table_error(error):
            _log_missing_table_once(error)
            return _empty_shadow_clone_model_config()
        raise

    return _serialize_shadow_clone_model_config_row(row)


async def upsert_shadow_clone_model_config(
    *,
    main_model_name: str,
    subagent_model_name: str,
    updated_by: str,
    client: Any = None,
) -> Dict[str, Optional[str]]:
    resolved_client = await _get_client(client)
    updated_by_uuid = UUID(str(updated_by))

    async def _perform_upsert() -> Any:
        async with resolved_client.pool.acquire() as conn:
            return await conn.fetchrow(
                """
                INSERT INTO shadow_clone_model_config (
                    config_key,
                    main_model_name,
                    subagent_model_name,
                    updated_by,
                    updated_at
                )
                VALUES ($1, $2, $3, $4, timezone('utc', now()))
                ON CONFLICT (config_key) DO UPDATE
                SET
                    main_model_name = EXCLUDED.main_model_name,
                    subagent_model_name = EXCLUDED.subagent_model_name,
                    updated_by = EXCLUDED.updated_by,
                    updated_at = timezone('utc', now())
                RETURNING main_model_name, subagent_model_name, updated_by, updated_at
                """,
                SHADOW_CLONE_MODEL_CONFIG_KEY,
                main_model_name,
                subagent_model_name,
                updated_by_uuid,
            )

    try:
        row = await _perform_upsert()
    except Exception as error:
        if not _is_missing_table_error(error):
            raise
        _log_missing_table_once(error)
        await _ensure_shadow_clone_model_config_table(resolved_client)
        row = await _perform_upsert()

    return _serialize_shadow_clone_model_config_row(row)
