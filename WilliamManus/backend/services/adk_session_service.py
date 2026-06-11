"""
Singleton/cached ADK session service.

Creating a new ADK DatabaseSessionService per request would create a new
SQLAlchemy async engine + connection pool each time, which is expensive and can
exhaust DB connections under high concurrency.

This module provides a per-process cached instance keyed by the async dialect
URL derived from the raw DATABASE_URL.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from utils.logger import logger
from utils.db_url import to_sqlalchemy_async_url, sanitize_db_url
from services.model_only_session_service import ModelOnlyDBSessionService


_service_cache: Dict[str, ModelOnlyDBSessionService] = {}
_cache_lock = asyncio.Lock()


async def get_adk_session_service(raw_db_url: str, **kwargs: Any) -> ModelOnlyDBSessionService:
    """
    Get a cached ModelOnlyDBSessionService.

    Args:
        raw_db_url: The raw DATABASE_URL (asyncpg-compatible).
        **kwargs: Passed through to ADK DatabaseSessionService.
    """
    async_db_url = to_sqlalchemy_async_url(raw_db_url)

    async with _cache_lock:
        existing = _service_cache.get(async_db_url)
        if existing is not None:
            return existing

        logger.info(
            "Creating cached ADK DatabaseSessionService for %s",
            sanitize_db_url(async_db_url),
        )
        service = ModelOnlyDBSessionService(raw_db_url, **kwargs)
        _service_cache[async_db_url] = service
        return service

