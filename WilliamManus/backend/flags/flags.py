"""
custom_agents = True       是否启用自定义Agent功能
mcp_module = True          是否启用MCP模块
templates_api = True       是否启用模板API
triggers_api = True        是否启用触发器API
workflows_api = True       是否启用工作流API
knowledge_base = True      是否启用知识库
pipedream = True           是否启用Pipedream集成
credentials_api = True     是否启用凭据API
default_agent = True       是否启用默认Agent
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from typing import Dict, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services import redis

logger = logging.getLogger(__name__)

class FeatureFlagManager:
    def __init__(self):
        """Initialize with existing Redis service."""
        self.flag_prefix = "feature_flag:"
        self.flag_list_key = "feature_flags:list"
        self.cache_ttl_seconds = float(os.getenv("FEATURE_FLAG_CACHE_TTL_SECONDS", "60"))
        self.redis_timeout_seconds = float(os.getenv("FEATURE_FLAG_REDIS_TIMEOUT_SECONDS", "1.5"))
        self.error_log_interval_seconds = float(
            os.getenv("FEATURE_FLAG_ERROR_LOG_INTERVAL_SECONDS", "15")
        )
        self._enabled_cache: Dict[str, tuple[bool, float]] = {}
        self._details_cache: Dict[str, tuple[Optional[Dict[str, str]], float]] = {}
        self._last_error_log_at = 0.0

    def _cache_get_enabled(self, key: str, *, allow_stale: bool = False) -> Optional[bool]:
        cached = self._enabled_cache.get(key)
        if not cached:
            return None
        value, updated_at = cached
        age = time.monotonic() - updated_at
        if age <= self.cache_ttl_seconds or allow_stale:
            return value
        return None

    def _cache_get_details(
        self,
        key: str,
        *,
        allow_stale: bool = False,
    ) -> Optional[Optional[Dict[str, str]]]:
        cached = self._details_cache.get(key)
        if not cached:
            return None
        value, updated_at = cached
        age = time.monotonic() - updated_at
        if age <= self.cache_ttl_seconds or allow_stale:
            return value
        return None

    def _cache_put_enabled(self, key: str, value: bool) -> None:
        self._enabled_cache[key] = (value, time.monotonic())

    def _cache_put_details(self, key: str, value: Optional[Dict[str, str]]) -> None:
        self._details_cache[key] = (value, time.monotonic())

    def _log_redis_error(self, message: str, error: Exception) -> None:
        now = time.monotonic()
        if now - self._last_error_log_at >= self.error_log_interval_seconds:
            self._last_error_log_at = now
            logger.error("%s: %s", message, error)
        else:
            logger.debug("%s: %s", message, error)

    async def set_flag(self, key: str, enabled: bool, description: str = "") -> bool:
        """Set a feature flag to enabled or disabled."""
        try:
            flag_key = f"{self.flag_prefix}{key}"
            flag_data = {
                "enabled": str(enabled).lower(),
                "description": description,
                "updated_at": datetime.utcnow().isoformat(),
            }

            await asyncio.wait_for(
                redis.hset(flag_key, mapping=flag_data),
                timeout=self.redis_timeout_seconds,
            )
            await asyncio.wait_for(
                redis.sadd(self.flag_list_key, key),
                timeout=self.redis_timeout_seconds,
            )

            self._cache_put_enabled(key, enabled)
            self._cache_put_details(key, flag_data)
            logger.info("Set feature flag %s to %s", key, enabled)
            return True
        except Exception as e:
            self._log_redis_error(f"Failed to set feature flag {key}", e)
            return False

    async def is_enabled(self, key: str) -> bool:
        """Check if a feature flag is enabled."""
        cached = self._cache_get_enabled(key)
        if cached is not None:
            return cached

        flag_key = f"{self.flag_prefix}{key}"
        try:
            enabled = await asyncio.wait_for(
                redis.hget(flag_key, "enabled"),
                timeout=self.redis_timeout_seconds,
            )
            result = enabled == "true" if enabled else False
            self._cache_put_enabled(key, result)
            return result
        except Exception as e:
            self._log_redis_error(f"❌ [FLAGS] Failed to check feature flag {key}", e)
            stale = self._cache_get_enabled(key, allow_stale=True)
            return stale if stale is not None else False

    async def get_flag(self, key: str) -> Optional[Dict[str, str]]:
        """Get feature flag details."""
        cached = self._cache_get_details(key)
        if cached is not None:
            return cached

        flag_key = f"{self.flag_prefix}{key}"
        try:
            flag_data = await asyncio.wait_for(
                redis.hgetall(flag_key),
                timeout=self.redis_timeout_seconds,
            )
            normalized = flag_data if flag_data else None
            self._cache_put_details(key, normalized)
            if normalized and "enabled" in normalized:
                self._cache_put_enabled(key, normalized["enabled"] == "true")
            return normalized
        except Exception as e:
            self._log_redis_error(f"Failed to get feature flag {key}", e)
            stale = self._cache_get_details(key, allow_stale=True)
            return stale

    async def delete_flag(self, key: str) -> bool:
        """Delete a feature flag."""
        try:
            flag_key = f"{self.flag_prefix}{key}"
            deleted = await asyncio.wait_for(
                redis.delete(flag_key),
                timeout=self.redis_timeout_seconds,
            )
            if deleted:
                await asyncio.wait_for(
                    redis.srem(self.flag_list_key, key),
                    timeout=self.redis_timeout_seconds,
                )
                self._enabled_cache.pop(key, None)
                self._details_cache.pop(key, None)
                logger.info("Deleted feature flag: %s", key)
                return True
            return False
        except Exception as e:
            self._log_redis_error(f"Failed to delete feature flag {key}", e)
            return False

    async def list_flags(self) -> Dict[str, bool]:
        """List all feature flags with their status."""
        try:
            flag_keys = await asyncio.wait_for(
                redis.smembers(self.flag_list_key),
                timeout=self.redis_timeout_seconds,
            )
            flags: Dict[str, bool] = {}
            for key in flag_keys:
                flags[key] = await self.is_enabled(key)
            return flags
        except Exception as e:
            self._log_redis_error("Failed to list feature flags", e)
            fallback: Dict[str, bool] = {}
            for key, (value, _) in self._enabled_cache.items():
                fallback[key] = value
            return fallback

    async def get_all_flags_details(self) -> Dict[str, Dict[str, str]]:
        """Get all feature flags with detailed information."""
        try:
            flag_keys = await asyncio.wait_for(
                redis.smembers(self.flag_list_key),
                timeout=self.redis_timeout_seconds,
            )
            flags: Dict[str, Dict[str, str]] = {}
            for key in flag_keys:
                flag_data = await self.get_flag(key)
                if flag_data:
                    flags[key] = flag_data
            return flags
        except Exception as e:
            self._log_redis_error("Failed to get all flags details", e)
            fallback: Dict[str, Dict[str, str]] = {}
            for key, (value, _) in self._details_cache.items():
                if value:
                    fallback[key] = value
            return fallback


_flag_manager: Optional[FeatureFlagManager] = None


def get_flag_manager() -> FeatureFlagManager:
    """Get the global feature flag manager instance"""
    global _flag_manager
    if _flag_manager is None:
        _flag_manager = FeatureFlagManager()
    return _flag_manager


# Async convenience functions
async def set_flag(key: str, enabled: bool, description: str = "") -> bool:
    return await get_flag_manager().set_flag(key, enabled, description)


async def is_enabled(key: str) -> bool:
    return await get_flag_manager().is_enabled(key)


async def enable_flag(key: str, description: str = "") -> bool:
    return await set_flag(key, True, description)


async def disable_flag(key: str, description: str = "") -> bool:
    return await set_flag(key, False, description)


async def delete_flag(key: str) -> bool:
    return await get_flag_manager().delete_flag(key)


async def list_flags() -> Dict[str, bool]:
    return await get_flag_manager().list_flags()


async def get_flag_details(key: str) -> Optional[Dict[str, str]]:
    return await get_flag_manager().get_flag(key)


# Feature Flags

# Fufanmanus default agent feature flag
fufanmanus_default_agent = True

# Custom agents feature flag
custom_agents = True

# MCP module feature flag  
mcp_module = False

# Templates API feature flag
templates_api = False

# Triggers API feature flag
triggers_api = False

# Workflows API feature flag
workflows_api = False

# Knowledge base feature flag
knowledge_base = False

# Pipedream integration feature flag
pipedream = False

# Credentials API feature flag
credentials_api = False





