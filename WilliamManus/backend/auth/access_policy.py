from typing import Any, Dict, Mapping

from fastapi import HTTPException

from services.postgresql import DBConnection


ROYS_ALPHA_INTERNAL_EMAIL_SUFFIX = "@iroys.cn"
db = DBConnection()


def user_has_roys_alpha_access(user_data: Mapping[str, Any]) -> bool:
    email = str(user_data.get("email") or "").strip().lower()
    role = str(user_data.get("role") or "user").strip().lower()
    status = str(user_data.get("status") or "").strip().lower()

    if status != "active":
        return False

    return role == "admin" or email.endswith(ROYS_ALPHA_INTERNAL_EMAIL_SUFFIX)


def build_user_capabilities(user_data: Mapping[str, Any]) -> Dict[str, bool]:
    return {
        "can_access_roys_alpha": user_has_roys_alpha_access(user_data),
    }


async def _get_client():
    await db.initialize()
    return await db.client


async def get_user_identity(user_id: str) -> Dict[str, Any]:
    client = await _get_client()

    async with client.pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, email, name, role, status, created_at
            FROM users
            WHERE id = $1
            """,
            user_id,
        )

    if not row:
        raise HTTPException(status_code=401, detail="User not found")

    user_data = dict(row)
    user_data["id"] = str(user_data.get("id"))
    return user_data


async def require_roys_alpha_access(user_id: str) -> Dict[str, Any]:
    user = await get_user_identity(user_id)

    if not user_has_roys_alpha_access(user):
        raise HTTPException(status_code=403, detail="Resource unavailable")

    return user
