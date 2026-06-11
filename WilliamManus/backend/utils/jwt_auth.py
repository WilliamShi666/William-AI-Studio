"""
JWT认证工具函数
处理JWT token的生成、验证、刷新等功能
"""

import jwt
import uuid
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, Tuple
from fastapi import HTTPException
from utils.config import config
from utils.logger import logger
import bcrypt

# JWT配置
UNSAFE_JWT_SECRETS = {
    "-".join(parts)
    for parts in (
        ("your", "secret", "key", "change", "in", "production"),
        ("your", "secret", "key", "change", "this", "in", "production"),
        ("your", "super", "secret", "jwt", "key", "change", "this", "in", "production"),
    )
}
JWT_SECRET_KEY = getattr(config, 'JWT_SECRET_KEY', None)
JWT_ALGORITHM = getattr(config, 'JWT_ALGORITHM', "HS256")
ACCESS_TOKEN_EXPIRE_MINUTES = getattr(config, 'ACCESS_TOKEN_EXPIRE_MINUTES', 60)  # 1小时
REFRESH_TOKEN_EXPIRE_DAYS = getattr(config, 'REFRESH_TOKEN_EXPIRE_DAYS', 30)    # 30天


def _require_jwt_secret() -> str:
    if not JWT_SECRET_KEY or JWT_SECRET_KEY in UNSAFE_JWT_SECRETS or len(JWT_SECRET_KEY) < 32:
        raise RuntimeError(
            "JWT_SECRET_KEY must be configured with a strong random value "
            "before JWT tokens can be signed or verified."
        )
    return JWT_SECRET_KEY

class JWTAuth:
    """JWT认证工具类"""
    
    @staticmethod
    def hash_password(password: str) -> str:
        """哈希密码"""
        salt = bcrypt.gensalt()
        return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')
    
    @staticmethod
    def verify_password(password: str, hashed: str) -> bool:
        """验证密码"""
        try:
            return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))
        except Exception as e:
            logger.error(f"Password verification error: {e}")
            return False
    
    @staticmethod
    def generate_token_pair(user_id: str, email: str, name: Optional[str] = None) -> Tuple[str, str, datetime]:
        """
        生成访问token和刷新token对
        
        Returns:
            Tuple[access_token, refresh_token, expires_at]
        """
        now = datetime.now(timezone.utc)
        
        # 生成JTI (JWT ID) - 用于追踪和撤销token
        jti = str(uuid.uuid4())
        
        # 访问token载荷
        access_payload = {
            "sub": user_id,  # 用户ID
            "email": email,
            "name": name,
            "jti": jti,  # JWT ID
            "type": "access",
            "iat": now.timestamp(),  # 签发时间
            "exp": (now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp(),  # 过期时间
        }
        
        # 生成访问token
        access_token = jwt.encode(access_payload, _require_jwt_secret(), algorithm=JWT_ALGORITHM)
        
        # 生成刷新token（简单的随机字符串）
        refresh_token = secrets.token_urlsafe(32)
        
        # 计算过期时间
        expires_at = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
        
        return access_token, refresh_token, expires_at
    
    @staticmethod
    def verify_access_token(token: str) -> Dict[str, Any]:
        """
        验证访问token
        
        Args:
            token: JWT访问token
            
        Returns:
            Dict包含用户信息
            
        Raises:
            HTTPException: token无效或过期
        """
        try:
            # 解码并验证token
            payload = jwt.decode(token, _require_jwt_secret(), algorithms=[JWT_ALGORITHM])
            
            # 检查token类型
            if payload.get("type") != "access":
                raise HTTPException(status_code=401, detail="Invalid token type")
            
            # 检查必需字段
            user_id = payload.get("sub")
            if not user_id:
                raise HTTPException(status_code=401, detail="Invalid token payload")
            
            # 检查是否过期
            exp = payload.get("exp")
            if exp and datetime.fromtimestamp(exp, timezone.utc) < datetime.now(timezone.utc):
                raise HTTPException(status_code=401, detail="Token expired")
            
            return {
                "user_id": user_id,
                "email": payload.get("email"),
                "name": payload.get("name"),
                "jti": payload.get("jti"),
                "expires_at": datetime.fromtimestamp(exp, timezone.utc) if exp else None
            }
            
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError as e:
            logger.warning(f"Invalid JWT token: {e}")
            raise HTTPException(status_code=401, detail="Invalid token")
        except Exception as e:
            logger.error(f"Token verification error: {e}")
            raise HTTPException(status_code=401, detail="Token verification failed")
    
    @staticmethod
    def hash_refresh_token(refresh_token: str) -> str:
        """哈希刷新token用于数据库存储"""
        return hashlib.sha256(refresh_token.encode()).hexdigest()
    
    @staticmethod
    def is_token_expired(expires_at: datetime) -> bool:
        """检查token是否过期"""
        return datetime.now(timezone.utc) >= expires_at
    
    @staticmethod
    def get_token_expiry() -> datetime:
        """获取新token的过期时间"""
        return datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    @staticmethod
    def get_refresh_token_expiry() -> datetime:
        """获取刷新token的过期时间"""
        return datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)

class TokenManager:
    """Token管理器，用于处理token相关的数据库操作"""
    
    def __init__(self, db_client):
        self.db = db_client
    
    async def store_refresh_token(self, user_id: str, refresh_token: str, expires_at: datetime) -> str:
        """
        存储刷新token到数据库
        
        Returns:
            refresh_token_id
        """
        token_hash = JWTAuth.hash_refresh_token(refresh_token)
        
        result = await self.db.table('auth_refresh_tokens').insert({
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": expires_at.isoformat(),
            "is_revoked": False
        }).execute()
        
        if result.data:
            return result.data[0]['id']
        else:
            raise HTTPException(status_code=500, detail="Failed to store refresh token")
    
    async def verify_refresh_token(self, refresh_token: str) -> Optional[Dict[str, Any]]:
        """
        验证刷新token并返回用户信息
        
        Args:
            refresh_token: 刷新token
            
        Returns:
            包含用户信息的字典，如果token无效则返回None
        """
        token_hash = JWTAuth.hash_refresh_token(refresh_token)
        
        try:
            # 查询刷新token
            result = await self.db.table('auth_refresh_tokens').select(
                'id, user_id, expires_at, is_revoked, auth_users!inner(id, email, name, status)'
            ).eq('token_hash', token_hash).eq('is_revoked', False).execute()
            
            if not result.data:
                return None
            
            token_data = result.data[0]
            user_data = token_data['auth_users']
            
            # 检查token是否过期
            expires_at = datetime.fromisoformat(token_data['expires_at'].replace('Z', '+00:00'))
            if JWTAuth.is_token_expired(expires_at):
                # 自动清理过期token
                await self.revoke_refresh_token(refresh_token)
                return None
            
            # 检查用户状态
            if user_data['status'] not in ['active']:
                return None
            
            # 更新最后使用时间
            await self.db.table('auth_refresh_tokens').update({
                'last_used_at': datetime.now(timezone.utc).isoformat()
            }).eq('id', token_data['id']).execute()
            
            return {
                "user_id": user_data['id'],
                "email": user_data['email'],
                "name": user_data['name'],
                "refresh_token_id": token_data['id']
            }
            
        except Exception as e:
            logger.error(f"Refresh token verification error: {e}")
            return None
    
    async def revoke_refresh_token(self, refresh_token: str) -> bool:
        """撤销刷新token"""
        token_hash = JWTAuth.hash_refresh_token(refresh_token)
        
        try:
            result = await self.db.table('auth_refresh_tokens').update({
                'is_revoked': True
            }).eq('token_hash', token_hash).execute()
            
            return len(result.data) > 0
        except Exception as e:
            logger.error(f"Failed to revoke refresh token: {e}")
            return False
    
    async def revoke_user_tokens(self, user_id: str) -> bool:
        """撤销用户的所有刷新token"""
        try:
            result = await self.db.table('auth_refresh_tokens').update({
                'is_revoked': True
            }).eq('user_id', user_id).execute()
            
            return True
        except Exception as e:
            logger.error(f"Failed to revoke user tokens: {e}")
            return False
    
    async def cleanup_expired_tokens(self) -> int:
        """清理过期的token"""
        try:
            now = datetime.now(timezone.utc).isoformat()
            result = await self.db.table('auth_refresh_tokens').delete().lt('expires_at', now).execute()
            
            count = len(result.data) if result.data else 0
            logger.info(f"Cleaned up {count} expired refresh tokens")
            return count
        except Exception as e:
            logger.error(f"Failed to cleanup expired tokens: {e}")
            return 0
