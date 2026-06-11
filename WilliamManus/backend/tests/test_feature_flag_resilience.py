import asyncio

import pytest

from flags import flags as feature_flags


@pytest.mark.asyncio
async def test_is_enabled_uses_stale_cache_when_redis_times_out(monkeypatch):
    manager = feature_flags.FeatureFlagManager()
    manager._cache_put_enabled("custom_agents", True)

    async def _raise_timeout(*args, **kwargs):
        raise asyncio.TimeoutError("redis timeout")

    monkeypatch.setattr(feature_flags.redis, "hget", _raise_timeout)

    assert await manager.is_enabled("custom_agents") is True


@pytest.mark.asyncio
async def test_get_flag_uses_stale_cache_when_redis_errors(monkeypatch):
    manager = feature_flags.FeatureFlagManager()
    cached_details = {
        "enabled": "true",
        "description": "cached",
        "updated_at": "2026-02-10T00:00:00Z",
    }
    manager._cache_put_details("custom_agents", cached_details)

    async def _raise_error(*args, **kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(feature_flags.redis, "hgetall", _raise_error)

    result = await manager.get_flag("custom_agents")
    assert result == cached_details
