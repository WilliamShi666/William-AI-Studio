from __future__ import annotations

import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


auth_utils_mod = types.ModuleType("utils.auth_utils")


class _StubAuthUtils:
    def verify_token(self, _token: str):
        raise AssertionError("verify_token should be patched in tests")


async def _verify_admin_api_key(*_args, **_kwargs):
    return True


auth_utils_mod.AuthUtils = _StubAuthUtils
auth_utils_mod.verify_admin_api_key = _verify_admin_api_key
sys.modules["utils.auth_utils"] = auth_utils_mod


suna_service_mod = types.ModuleType("utils.suna_default_agent_service")


class _StubSunaDefaultAgentService:
    async def install_suna_agent_for_user(self, *_args, **_kwargs):
        raise AssertionError("install_suna_agent_for_user should not be called in these tests")


suna_service_mod.SunaDefaultAgentService = _StubSunaDefaultAgentService
sys.modules["utils.suna_default_agent_service"] = suna_service_mod


from admin import api as admin_api


class _AcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append(("fetchrow", query, args))
        if not self._responses:
            return None
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def execute(self, query, *args):
        self.calls.append(("execute", query, args))
        return "CREATE TABLE"


class _FakeClient:
    def __init__(self, conn):
        self.pool = SimpleNamespace(acquire=lambda: _AcquireContext(conn))


class _FakeDBConnection:
    def __init__(self, client):
        self._client = client

    async def initialize(self):
        return None

    @property
    async def client(self):
        return self._client


@pytest.mark.asyncio
async def test_require_admin_rejects_non_admin(monkeypatch):
    monkeypatch.setattr(
        admin_api.auth_utils,
        "verify_token",
        lambda _token: {"user_id": "user-1"},
    )

    async def _fake_user(_user_id: str):
        return {"id": "user-1", "role": "user", "status": "active"}

    monkeypatch.setattr(admin_api, "_get_user_by_id", _fake_user)

    with pytest.raises(HTTPException) as exc_info:
        await admin_api.require_admin(
            credentials=SimpleNamespace(credentials="token")
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Admin access required"


@pytest.mark.asyncio
async def test_get_shadow_clone_model_config_returns_empty_shape_when_missing(monkeypatch):
    conn = _FakeConn([None])
    monkeypatch.setattr(
        admin_api,
        "db",
        _FakeDBConnection(_FakeClient(conn)),
    )

    result = await admin_api.get_shadow_clone_model_config(
        _={"id": str(uuid4()), "role": "admin", "status": "active"}
    )

    assert result.main_model_name is None
    assert result.subagent_model_name is None
    assert result.updated_by is None
    assert result.updated_at is None
    assert conn.calls[0][2] == ("global",)


@pytest.mark.asyncio
async def test_get_shadow_clone_model_config_returns_empty_shape_when_table_missing(monkeypatch):
    conn = _FakeConn([Exception('relation "shadow_clone_model_config" does not exist')])
    monkeypatch.setattr(
        admin_api,
        "db",
        _FakeDBConnection(_FakeClient(conn)),
    )

    result = await admin_api.get_shadow_clone_model_config(
        _={"id": str(uuid4()), "role": "admin", "status": "active"}
    )

    assert result.main_model_name is None
    assert result.subagent_model_name is None
    assert result.updated_by is None
    assert result.updated_at is None


@pytest.mark.asyncio
async def test_save_shadow_clone_model_config_persists_trimmed_values(monkeypatch):
    admin_user_id = str(uuid4())
    updated_at = datetime(2026, 3, 25, 8, 50, tzinfo=timezone.utc)
    conn = _FakeConn([
        {
            "main_model_name": "kimi-k2.5",
            "subagent_model_name": "openrouter/minimax/minimax-m2.5",
            "updated_by": admin_user_id,
            "updated_at": updated_at,
        }
    ])
    monkeypatch.setattr(
        admin_api,
        "db",
        _FakeDBConnection(_FakeClient(conn)),
    )

    result = await admin_api.save_shadow_clone_model_config(
        request=admin_api.ShadowCloneModelConfigUpdateRequest(
            main_model_name="  moonshotai/kimi-k2.5  ",
            subagent_model_name="  openrouter/minimax/minimax-m2.5  ",
        ),
        current_user={"id": admin_user_id, "role": "admin", "status": "active"},
    )

    assert result.main_model_name == "kimi-k2.5"
    assert result.subagent_model_name == "openrouter/minimax/minimax-m2.5"
    assert result.updated_by == admin_user_id
    assert result.updated_at == updated_at.isoformat()

    _, _, args = conn.calls[0]
    assert args[0] == "global"
    assert args[1] == "kimi-k2.5"
    assert args[2] == "openrouter/minimax/minimax-m2.5"
    assert str(args[3]) == admin_user_id


@pytest.mark.asyncio
async def test_save_shadow_clone_model_config_accepts_new_shadow_clone_models(monkeypatch):
    admin_user_id = str(uuid4())
    updated_at = datetime(2026, 3, 25, 10, 20, tzinfo=timezone.utc)
    conn = _FakeConn([
        {
            "main_model_name": "kimi-k2.6",
            "subagent_model_name": "deepseek-v4-pro-max",
            "updated_by": admin_user_id,
            "updated_at": updated_at,
        }
    ])
    monkeypatch.setattr(
        admin_api,
        "db",
        _FakeDBConnection(_FakeClient(conn)),
    )

    result = await admin_api.save_shadow_clone_model_config(
        request=admin_api.ShadowCloneModelConfigUpdateRequest(
            main_model_name="  moonshotai/kimi-k2.6  ",
            subagent_model_name="  deepseek-v4-pro-max  ",
        ),
        current_user={"id": admin_user_id, "role": "admin", "status": "active"},
    )

    assert result.main_model_name == "kimi-k2.6"
    assert result.subagent_model_name == "deepseek-v4-pro-max"

    _, _, args = conn.calls[0]
    assert args[1] == "kimi-k2.6"
    assert args[2] == "deepseek-v4-pro-max"


@pytest.mark.asyncio
async def test_save_shadow_clone_model_config_accepts_new_openrouter_models(monkeypatch):
    admin_user_id = str(uuid4())
    updated_at = datetime(2026, 3, 25, 10, 25, tzinfo=timezone.utc)
    conn = _FakeConn([
        {
            "main_model_name": "openrouter/moonshotai/kimi-k2.6",
            "subagent_model_name": "openrouter/deepseek/deepseek-v4-flash",
            "updated_by": admin_user_id,
            "updated_at": updated_at,
        }
    ])
    monkeypatch.setattr(
        admin_api,
        "db",
        _FakeDBConnection(_FakeClient(conn)),
    )

    result = await admin_api.save_shadow_clone_model_config(
        request=admin_api.ShadowCloneModelConfigUpdateRequest(
            main_model_name="openrouter/moonshotai/kimi-k2.6",
            subagent_model_name="openrouter/deepseek/deepseek-v4-flash",
        ),
        current_user={"id": admin_user_id, "role": "admin", "status": "active"},
    )

    assert result.main_model_name == "openrouter/moonshotai/kimi-k2.6"
    assert result.subagent_model_name == "openrouter/deepseek/deepseek-v4-flash"


@pytest.mark.asyncio
async def test_save_shadow_clone_model_config_creates_table_when_missing(monkeypatch):
    admin_user_id = str(uuid4())
    updated_at = datetime(2026, 3, 25, 9, 10, tzinfo=timezone.utc)
    conn = _FakeConn([
        Exception('relation "shadow_clone_model_config" does not exist'),
        {
            "main_model_name": "kimi-k2.5",
            "subagent_model_name": "openrouter/minimax/minimax-m2.5",
            "updated_by": admin_user_id,
            "updated_at": updated_at,
        },
    ])
    monkeypatch.setattr(
        admin_api,
        "db",
        _FakeDBConnection(_FakeClient(conn)),
    )

    result = await admin_api.save_shadow_clone_model_config(
        request=admin_api.ShadowCloneModelConfigUpdateRequest(
            main_model_name="kimi-k2.5",
            subagent_model_name="openrouter/minimax/minimax-m2.5",
        ),
        current_user={"id": admin_user_id, "role": "admin", "status": "active"},
    )

    assert result.main_model_name == "kimi-k2.5"
    assert result.subagent_model_name == "openrouter/minimax/minimax-m2.5"
    assert any(call[0] == "execute" for call in conn.calls)


@pytest.mark.asyncio
async def test_save_shadow_clone_model_config_rejects_blank_model_name():
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.save_shadow_clone_model_config(
            request=admin_api.ShadowCloneModelConfigUpdateRequest(
                main_model_name="   ",
                subagent_model_name="openrouter/minimax/minimax-m2.5",
            ),
            current_user={"id": str(uuid4()), "role": "admin", "status": "active"},
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "main_model_name is required"


@pytest.mark.asyncio
async def test_save_shadow_clone_model_config_rejects_qwen_shadow_clone_model():
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.save_shadow_clone_model_config(
            request=admin_api.ShadowCloneModelConfigUpdateRequest(
                main_model_name="dashscope/qwen3.5-plus",
                subagent_model_name="openrouter/minimax/minimax-m2.5",
            ),
            current_user={"id": str(uuid4()), "role": "admin", "status": "active"},
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "main_model_name must be an official Kimi/DeepSeek model or an openrouter/... model"
    )


@pytest.mark.asyncio
async def test_save_shadow_clone_model_config_rejects_ppio_shadow_clone_model():
    with pytest.raises(HTTPException) as exc_info:
        await admin_api.save_shadow_clone_model_config(
            request=admin_api.ShadowCloneModelConfigUpdateRequest(
                main_model_name="kimi-k2.5",
                subagent_model_name="ppio/moonshotai/kimi-k2.5",
            ),
            current_user={"id": str(uuid4()), "role": "admin", "status": "active"},
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "subagent_model_name must be an official Kimi/DeepSeek model or an openrouter/... model"
    )
