"""
Helpers for dealing with database URLs across different async libraries.

This backend uses asyncpg directly, which expects a normal Postgres URL like:
    postgresql://user:pass@host:port/db

Google ADK's DatabaseSessionService uses SQLAlchemy asyncio, which requires an
async dialect, e.g.:
    postgresql+asyncpg://user:pass@host:port/db

These helpers keep the raw DATABASE_URL compatible with asyncpg while safely
deriving an async SQLAlchemy URL for ADK.
"""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse


def to_sqlalchemy_async_url(raw_url: str) -> str:
    """
    Convert a raw database URL into a SQLAlchemy asyncio-compatible URL.

    Rules:
    - If the scheme already specifies a driver (contains '+'), return as-is.
    - postgres:// or postgresql:// -> postgresql+asyncpg://
    - sqlite:///... -> sqlite+aiosqlite:///...
    - Otherwise, return as-is.
    """
    if not raw_url:
        return raw_url

    parsed = urlparse(raw_url)
    scheme = parsed.scheme

    # Already explicit about a driver (sync or async). Do not rewrite.
    if "+" in scheme:
        return raw_url

    if scheme in ("postgresql", "postgres"):
        return urlunparse(parsed._replace(scheme="postgresql+asyncpg"))

    if scheme == "sqlite":
        # Preserve the original slash count for sqlite URLs (SQLAlchemy cares).
        if raw_url.startswith("sqlite://"):
            return raw_url.replace("sqlite://", "sqlite+aiosqlite://", 1)
        return urlunparse(parsed._replace(scheme="sqlite+aiosqlite"))

    return raw_url


def sanitize_db_url(db_url: str) -> str:
    """Hide password in a DB URL for safe logging."""
    if not db_url:
        return db_url

    parsed = urlparse(db_url)
    if parsed.password:
        safe_netloc = parsed.netloc.replace(parsed.password, "********")
        parsed = parsed._replace(netloc=safe_netloc)
    return urlunparse(parsed)
