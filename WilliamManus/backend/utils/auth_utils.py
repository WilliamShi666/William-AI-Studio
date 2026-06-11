"""
JSON Web Token
"""

import jwt # type: ignore
# import PyJWT as jwt # type: ignore
import bcrypt # type: ignore
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from fastapi import HTTPException, Header # type: ignore
from utils.config import config
from utils.logger import logger

UNSAFE_JWT_SECRETS = {
    "-".join(parts)
    for parts in (
        ("your", "secret", "key", "change", "in", "production"),
        ("your", "secret", "key", "change", "this", "in", "production"),
        ("your", "super", "secret", "jwt", "key", "change", "this", "in", "production"),
    )
}


def _require_jwt_secret(secret: Optional[str]) -> str:
    if not secret or secret in UNSAFE_JWT_SECRETS or len(secret) < 32:
        raise RuntimeError(
            "JWT_SECRET_KEY must be configured with a strong random value "
            "before JWT tokens can be signed or verified."
        )
    return secret


class AuthUtils:
    """JWT Authentication Utilities"""
    
    # JWT配置
    # 生产配置：这里需要 使用加密安全的随机生成器生成，可以通过环境变量或者外部服务管理秘钥，并做定期轮换
    # 硬编码仅使用课程演示环境或者研发测试环境
    JWT_SECRET = getattr(config, 'JWT_SECRET_KEY', None)
    ACCESS_TOKEN_EXPIRE_HOURS = 24  # 普通接口访问携带的token过期时间为24小时
    REFRESH_TOKEN_EXPIRE_DAYS = 30  # 刷新token过期时间为30天
    
    @classmethod
    def initialize(cls):
        """Initialize authentication tool and record configuration information"""

        # 验证JWT密钥
        _require_jwt_secret(cls.JWT_SECRET)
        logger.info("JWT secret key is configured")
    
    @staticmethod
    def hash_password(password: str) -> str:
        """password hashing"""
        try:
            hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
            return hashed
        except Exception as e:
            logger.error(f"Password hashing failed: {e}")
            raise
    
    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """密码验证"""
        try:
            is_valid = bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
            if is_valid:
                logger.info("Password verification successful")
            else:
                logger.warning("Password verification failed: invalid password")
            return is_valid
        except Exception as e:
            logger.error(f"Password verification error: {e}")
            return False
    
    @staticmethod
    def create_access_token(
        user_id: str,
        role: str = "user",
        can_access_roys_alpha: bool = False,
    ) -> str:
        """Create access token
        
        # 传统Session方式（有状态）
        # 服务器需要存储每个用户的登录状态
        sessions = {
            "session_123": {"user_id": "user_456", "login_time": "2024-01-01"}
        }

        # JWT方式（无状态）
        # 所有信息都编码在token中，服务器无需存储状态
        token = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
        
        """
        try:
            expire = datetime.now(timezone.utc) + timedelta(hours=AuthUtils.ACCESS_TOKEN_EXPIRE_HOURS)
            payload = {
                "sub": user_id,   # Subject - 主题(用户ID)
                "role": role,
                "can_access_roys_alpha": can_access_roys_alpha,
                "exp": expire,    # Expiration time - 过期时间
                "iat": datetime.now(timezone.utc),  # Issued at - 发行时间
                "type": "access"  # Custom - 自定义字段
            }

            # 使用标准HS256算法
            token = jwt.encode(payload, _require_jwt_secret(AuthUtils.JWT_SECRET), algorithm="HS256")
            logger.info(f"Created access token for user {user_id}, expires at {expire}")
            return token
        except Exception as e:
            logger.error(f"Failed to create access token for user {user_id}: {e}")
            raise
    
    @staticmethod
    def create_refresh_token() -> str:
        """Create refresh token
        Access Token过期时 或者 前端自动请求刷新token时触发
        1. 验证refresh_token是否有效
        2. 删除旧的refresh_token  
        3. 生成新的access_token和refresh_token
        4. 返回新的token对
        """
        try:
            token = secrets.token_urlsafe(32)
            logger.info(f"Created refresh token: {token[:8]}...")
            return token
        except Exception as e:
            logger.error(f"Failed to create refresh token: {e}")
            raise
    
    @staticmethod
    def verify_token(token: str) -> Dict[str, Any]:
        """Verify token"""
        try:
            logger.debug(f"Verifying token: {token[:10]}...")

            # Step 1: JWT结构验证和签名验证
            payload = jwt.decode(token, _require_jwt_secret(AuthUtils.JWT_SECRET), algorithms=["HS256"])
            
            # Step 2: 检查必需字段
            user_id = payload.get("sub")
            if not user_id:
                logger.warning(f"Token verification failed: missing user_id in payload")
                raise HTTPException(status_code=401, detail="Invalid token")
            logger.info(f"Token verified successfully for user {user_id}")

            # Step 3: 自动检查过期时间（jwt.decode会自动检查exp字段）
            # 如果过期会抛出jwt.ExpiredSignatureError
            return {
                "user_id": user_id,
                "role": payload.get("role", "user"),
                "can_access_roys_alpha": bool(
                    payload.get("can_access_roys_alpha", False)
                ),
                "payload": payload
            }
        except jwt.ExpiredSignatureError:
            # Token过期
            logger.warning(f"Token verification failed: token expired")
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            # Token无效
            logger.warning(f"Token verification failed: invalid token")
            raise HTTPException(status_code=401, detail="Invalid token")
        except RuntimeError as e:
            logger.error(f"Token verification configuration error: {e}")
            raise HTTPException(status_code=500, detail="JWT secret is not configured safely")
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            raise HTTPException(status_code=401, detail="Token verification failed")
    
    @staticmethod
    def hash_refresh_token(token: str) -> str:
        """hash refresh token"""
        try:
            hashed = hashlib.sha256(token.encode()).hexdigest()
            logger.debug(f"Refresh token hashed: {hashed[:8]}...")
            return hashed
        except Exception as e:
            logger.error(f"Failed to hash refresh token: {e}")
            raise
    
    @staticmethod
    def get_refresh_token_expire_time() -> datetime:
        """get refresh token expire time"""
        try:
            expire_time = datetime.now(timezone.utc) + timedelta(days=AuthUtils.REFRESH_TOKEN_EXPIRE_DAYS)
            logger.debug(f"Refresh token expire time calculated: {expire_time}")
            return expire_time
        except Exception as e:
            logger.error(f"Failed to calculate refresh token expire time: {e}")
            raise
    
    @staticmethod
    def is_token_expired(token: str) -> bool:
        """check token is expired"""
        try:
            logger.debug(f"Checking token expiration: {token[:10]}...")
            payload = jwt.decode(token, _require_jwt_secret(AuthUtils.JWT_SECRET), algorithms=["HS256"])
            exp = payload.get("exp")
            if exp:
                is_expired = datetime.fromtimestamp(exp, tz=timezone.utc) < datetime.now(timezone.utc)
                if is_expired:
                    logger.info("Token is expired")
                else:
                    logger.debug("Token is still valid")
                return is_expired
            logger.warning("Token has no expiration time")
            return True
        except Exception as e:
            logger.error(f"Error checking token expiration: {e}")
            return True

    @staticmethod
    async def get_account_id_from_thread(client, thread_id: str) -> str:
        """
        Get account ID from thread ID
        
        Args:
            client: database client
            thread_id: thread ID
            
        Returns:
            str: USER ID
            
        Raises:
            HTTPException: if thread not found
        """
        try:
            thread_result = await client.table('threads').select('account_id').eq('thread_id', thread_id).execute()
            if not thread_result.data:
                logger.error(f"Thread not found: {thread_id}")
                raise HTTPException(status_code=404, detail="Thread not found")
            account_id = thread_result.data[0]['account_id']
            logger.debug(f"Retrieved account_id {account_id} for thread {thread_id}")
            return account_id
            
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error getting account_id from thread {thread_id}: {e}")
            raise HTTPException(status_code=500, detail=f"Error retrieving account information: {str(e)}")


async def verify_admin_api_key(x_admin_api_key: Optional[str] = Header(None)):
    """Verify admin API key (legacy admin endpoints)."""
    expected_key = getattr(config, "ADMIN_API_KEY", None) or os.getenv("ADMIN_API_KEY")
    if not expected_key:
        raise HTTPException(status_code=500, detail="Admin API key not configured")

    if not x_admin_api_key or x_admin_api_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid admin API key")

    return True
