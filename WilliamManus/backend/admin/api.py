from fastapi import APIRouter, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional, Dict, List
from pydantic import BaseModel
from services.shadow_clone_model_config import (
    get_shadow_clone_model_config as read_shadow_clone_model_config,
    normalize_shadow_clone_selected_model_name,
    upsert_shadow_clone_model_config,
)
from services.postgresql import DBConnection
from utils.auth_utils import verify_admin_api_key, AuthUtils
from utils.suna_default_agent_service import SunaDefaultAgentService
from utils.logger import logger
from utils.config import config, EnvMode
from dotenv import load_dotenv, set_key, find_dotenv, dotenv_values

router = APIRouter(prefix="/admin", tags=["admin"])
security = HTTPBearer(auto_error=False)
auth_utils = AuthUtils()
db = DBConnection()


class UserStats(BaseModel):
    total_users: int
    admin_count: int
    user_count: int
    active_users: int
    disabled_users: int


class UserListItem(BaseModel):
    id: str
    email: str
    name: str
    role: str
    status: str
    created_at: str


class StatusUpdate(BaseModel):
    status: str  # active | suspended


class ShadowCloneModelConfigResponse(BaseModel):
    main_model_name: Optional[str] = None
    subagent_model_name: Optional[str] = None
    updated_by: Optional[str] = None
    updated_at: Optional[str] = None


class ShadowCloneModelConfigUpdateRequest(BaseModel):
    main_model_name: str
    subagent_model_name: str


async def _get_client():
    await db.initialize()
    return await db.client


async def _get_user_by_id(user_id: str) -> Dict[str, str]:
    client = await _get_client()
    async with client.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, name, role, status, created_at
            FROM users
            WHERE id = $1
            """,
            user_id
        )
    if not row:
        raise HTTPException(status_code=401, detail="User not found")
    data = dict(row)
    data["id"] = str(data.get("id"))
    return data


async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> Dict[str, str]:
    if not credentials:
        raise HTTPException(status_code=401, detail="Authentication required")

    token_data = auth_utils.verify_token(credentials.credentials)
    user_id = token_data["user_id"]
    user = await _get_user_by_id(user_id)
    role = user.get("role") or token_data.get("role", "user")

    if user.get("status") != "active":
        raise HTTPException(status_code=403, detail="Account is not active")
    if role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")

    return user


def _require_supported_shadow_clone_model_name(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise HTTPException(status_code=400, detail=f"{field_name} is required")
    try:
        return normalize_shadow_clone_selected_model_name(normalized)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=f"{field_name} {error}") from error

@router.post("/suna-agents/install-user/{account_id}")
async def admin_install_suna_for_user(
    account_id: str,
    replace_existing: bool = False,
    _: bool = Depends(verify_admin_api_key)
):
    logger.info(f"Admin installing Suna agent for user: {account_id}")
    
    service = SunaDefaultAgentService()
    agent_id = await service.install_suna_agent_for_user(account_id, replace_existing)
    
    if agent_id:
        return {
            "success": True,
            "message": f"Successfully installed Suna agent for user {account_id}",
            "agent_id": agent_id
        }
    else:
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to install Suna agent for user {account_id}"
        )


@router.get("/users/stats", response_model=UserStats)
async def get_user_stats(_: Dict[str, str] = Depends(require_admin)):
    client = await _get_client()
    async with client.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*) AS total_users,
                COUNT(*) FILTER (WHERE role = 'admin') AS admin_count,
                COUNT(*) FILTER (WHERE role = 'user') AS user_count,
                COUNT(*) FILTER (WHERE status = 'active') AS active_users,
                COUNT(*) FILTER (WHERE status != 'active') AS disabled_users
            FROM users
            """
        )
    data = dict(row) if row else {}
    return UserStats(
        total_users=data.get("total_users", 0),
        admin_count=data.get("admin_count", 0),
        user_count=data.get("user_count", 0),
        active_users=data.get("active_users", 0),
        disabled_users=data.get("disabled_users", 0),
    )


@router.get("/users", response_model=List[UserListItem])
async def list_users(_: Dict[str, str] = Depends(require_admin)):
    client = await _get_client()
    async with client.pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, email, name, role, status, created_at
            FROM users
            ORDER BY created_at DESC
            """
        )
    users: List[UserListItem] = []
    for row in rows:
        user = dict(row)
        created_at = user.get("created_at")
        users.append(UserListItem(
            id=str(user.get("id")),
            email=user.get("email") or "",
            name=user.get("name") or "",
            role=user.get("role") or "user",
            status=user.get("status") or "inactive",
            created_at=created_at.isoformat() if created_at else "",
        ))
    return users


@router.patch("/users/{user_id}/status")
async def update_user_status(
    user_id: str,
    update: StatusUpdate,
    current_user: Dict[str, str] = Depends(require_admin),
):
    if update.status not in {"active", "suspended"}:
        raise HTTPException(status_code=400, detail="Invalid status")

    if user_id == current_user.get("id"):
        raise HTTPException(status_code=400, detail="Cannot update your own status")

    client = await _get_client()
    async with client.pool.acquire() as conn:
        result = await conn.execute(
            "UPDATE users SET status = $1 WHERE id = $2",
            update.status,
            user_id
        )
    updated = int(result.split()[-1]) if result else 0
    if updated == 0:
        raise HTTPException(status_code=404, detail="User not found")

    return {"status": "success"}


@router.get("/shadow-clone-model-config", response_model=ShadowCloneModelConfigResponse)
async def get_shadow_clone_model_config(
    _: Dict[str, str] = Depends(require_admin),
):
    client = await _get_client()
    config_data = await read_shadow_clone_model_config(client=client)
    return ShadowCloneModelConfigResponse(**config_data)


@router.post("/shadow-clone-model-config", response_model=ShadowCloneModelConfigResponse)
async def save_shadow_clone_model_config(
    request: ShadowCloneModelConfigUpdateRequest,
    current_user: Dict[str, str] = Depends(require_admin),
):
    main_model_name = _require_supported_shadow_clone_model_name(
        request.main_model_name,
        "main_model_name",
    )
    subagent_model_name = _require_supported_shadow_clone_model_name(
        request.subagent_model_name,
        "subagent_model_name",
    )
    client = await _get_client()
    config_data = await upsert_shadow_clone_model_config(
        client=client,
        main_model_name=main_model_name,
        subagent_model_name=subagent_model_name,
        updated_by=current_user["id"],
    )
    return ShadowCloneModelConfigResponse(**config_data)

@router.get("/env-vars")
def get_env_vars(_: Dict[str, str] = Depends(require_admin)) -> Dict[str, str]:
    """Get environment variables (local mode only)."""
    if config.ENV_MODE != EnvMode.LOCAL:
        raise HTTPException(status_code=403, detail="Env vars management only available in local mode")
    
    try:
        env_path = find_dotenv()
        if not env_path:
            logger.error("Could not find .env file")
            return {}
        
        return dotenv_values(env_path)
    except Exception as e:
        logger.error(f"Failed to get env vars: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to get env variables: {e}")

@router.post("/env-vars")
def save_env_vars(
    request: Dict[str, str],
    _: Dict[str, str] = Depends(require_admin),
) -> Dict[str, str]:
    """Save environment variables (local mode only)."""
    if config.ENV_MODE != EnvMode.LOCAL:
        raise HTTPException(status_code=403, detail="Env vars management only available in local mode")

    try:
        env_path = find_dotenv()
        if not env_path:
            raise HTTPException(status_code=500, detail="Could not find .env file")
        
        for key, value in request.items():
            set_key(env_path, key, value)
        
        load_dotenv(override=True)
        logger.info("Env variables saved successfully")
        return {"message": "Env variables saved successfully"}
    except Exception as e:
        logger.error(f"Failed to save env variables: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to save env variables: {e}") 
