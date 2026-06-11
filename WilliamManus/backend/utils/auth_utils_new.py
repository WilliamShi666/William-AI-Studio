"""
新的认证工具函数
支持本地JWT认证系统和API Key认证
"""

import sentry
from fastapi import HTTPException, Request, Header
from typing import Optional
import jwt
from jwt.exceptions import PyJWTError
from utils.logger import structlog
from utils.config import config
import os
from services.postgresql import DBConnection
from services import redis
from utils.jwt_auth import JWTAuth

async def _get_user_id_from_account_cached(account_id: str) -> Optional[str]:
    """
    Get user_id from account_id with Redis caching for performance
    
    Args:
        account_id: The account ID to look up
        
    Returns:
        str: The primary owner user ID, or None if not found
    """
    cache_key = f"account_user:{account_id}"
    
    try:
        # Check Redis cache first
        redis_client = await redis.get_client()
        cached_user_id = await redis_client.get(cache_key)
        if cached_user_id:
            return cached_user_id.decode('utf-8') if isinstance(cached_user_id, bytes) else cached_user_id
    except Exception as e:
        structlog.get_logger().warning(f"Redis cache lookup failed for account {account_id}: {e}")
    
    try:
        # Fallback to database
        db = DBConnection()
        await db.initialize()
        client = await db.client
        
        # 首先尝试从新的auth_users表查找
        user_result = await client.table('auth_users').select('id').eq('id', account_id).limit(1).execute()
        
        if user_result.data:
            user_id = user_result.data[0]['id']
            
            # Cache the result for 5 minutes
            try:
                await redis_client.setex(cache_key, 300, user_id)
            except Exception as e:
                structlog.get_logger().warning(f"Failed to cache user lookup: {e}")
                
            return user_id
        
        # 如果在auth_users表中没找到，尝试从basejump.accounts表查找
        user_result = await client.schema('basejump').table('accounts').select(
            'primary_owner_user_id'
        ).eq('id', account_id).limit(1).execute()
        
        if user_result.data:
            user_id = user_result.data[0]['primary_owner_user_id']
            
            # Cache the result for 5 minutes
            try:
                await redis_client.setex(cache_key, 300, user_id)
            except Exception as e:
                structlog.get_logger().warning(f"Failed to cache user lookup: {e}")
                
            return user_id
        
        return None
        
    except Exception as e:
        structlog.get_logger().error(f"Database lookup failed for account {account_id}: {e}")
        return None

# 新的JWT认证函数
async def get_current_user_id_from_jwt_new(request: Request) -> str:
    """
    Extract and verify the user ID from the new local JWT system or API key.
    
    This function supports both the new local JWT authentication and the existing API key system.
    
    Supports authentication via:
    1. X-API-Key header (public:secret key pairs from API keys table) 
    2. Authorization header with Bearer token (new local JWT)
    3. X-Refresh-Token header (optional, for token refresh indication)
    
    Args:
        request: The FastAPI request object
        
    Returns:
        str: The user ID extracted from the JWT or API key
        
    Raises:
        HTTPException: If no valid token is found or if the token is invalid
    """

    x_api_key = request.headers.get('x-api-key')

    # Check for user API keys in the database (existing functionality)
    if x_api_key:
        try:
            # Parse the API key format: "pk_xxx:sk_xxx"
            if ':' not in x_api_key:
                raise HTTPException(
                    status_code=401,
                    detail="Invalid API key format. Expected format: pk_xxx:sk_xxx",
                    headers={"WWW-Authenticate": "Bearer"}
                )
            
            public_key, secret_key = x_api_key.split(':', 1)
            
            from services.api_keys import APIKeyService
            db = DBConnection()
            await db.initialize()
            api_key_service = APIKeyService(db)
            
            validation_result = await api_key_service.validate_api_key(public_key, secret_key)
            
            if validation_result.is_valid:
                # Get user_id from account_id with caching
                user_id = await _get_user_id_from_account_cached(str(validation_result.account_id))
                
                if user_id:
                    sentry.sentry.set_user({ "id": user_id })
                    structlog.contextvars.bind_contextvars(
                        user_id=user_id,
                        auth_method="api_key",
                        api_key_id=str(validation_result.key_id),
                        public_key=public_key
                    )
                    return user_id
                else:
                    raise HTTPException(
                        status_code=401,
                        detail="API key account not found",
                        headers={"WWW-Authenticate": "Bearer"}
                    )
            else:
                raise HTTPException(
                    status_code=401,
                    detail=f"Invalid API key: {validation_result.error_message}",
                    headers={"WWW-Authenticate": "Bearer"}
                )

        except HTTPException:
            raise
        except Exception as e:
            structlog.get_logger().error(f"Error validating API key: {e}")
            raise HTTPException(
                status_code=401,
                detail="API key validation failed",
                headers={"WWW-Authenticate": "Bearer"}
            )

    # Fall back to new local JWT authentication
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        raise HTTPException(
            status_code=401,
            detail="No valid authentication credentials found",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    token = auth_header.split(' ')[1]
    
    try:
        # Use new JWT auth system
        token_data = JWTAuth.verify_access_token(token)
        user_id = token_data["user_id"]
        
        if not user_id:
            raise HTTPException(
                status_code=401,
                detail="Invalid token payload",
                headers={"WWW-Authenticate": "Bearer"}
            )

        sentry.sentry.set_user({ "id": user_id })
        structlog.contextvars.bind_contextvars(
            user_id=user_id,
            auth_method="local_jwt",
            token_jti=token_data.get("jti")
        )
        
        # Check for refresh token header (for frontend compatibility)
        refresh_token = request.headers.get('X-Refresh-Token')
        if refresh_token:
            structlog.contextvars.bind_contextvars(
                has_refresh_token=True
            )
        
        return user_id
        
    except HTTPException:
        raise
    except Exception as e:
        structlog.get_logger().error(f"JWT verification error: {e}")
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"}
        )

async def get_account_id_from_thread(client, thread_id: str) -> str:
    """
    Get account_id from thread_id
    """
    thread_result = await client.table('threads').select('account_id').eq('thread_id', thread_id).execute()
    if not thread_result.data:
        raise HTTPException(status_code=404, detail="Thread not found")
    return thread_result.data[0]['account_id']

async def verify_thread_access(client, thread_id: str, user_id: str):
    """
    Verify if a user has access to a specific thread.
    
    This function checks if the user is the owner of the thread or has been granted access
    through account sharing mechanisms.
    
    Args:
        client: The Supabase client
        thread_id: The thread ID to check access for
        user_id: The user ID to verify access for
        
    Raises:
        HTTPException: If the user doesn't have access to the thread
    """
    try:
        # Get thread details with account info
        thread_result = await client.table('threads').select('account_id').eq('thread_id', thread_id).execute()
        
        if not thread_result.data:
            raise HTTPException(status_code=404, detail="Thread not found")
        
        thread_account_id = thread_result.data[0]['account_id']
        
        # Check if user has access to this account
        # Direct ownership check: in this project, account_id directly stores user_id
        if thread_account_id == user_id:
            return

        # No basejump in this project - direct ownership is the only check needed
        raise HTTPException(status_code=403, detail="Access to this thread is forbidden")
        
    except HTTPException:
        raise
    except Exception as e:
        structlog.get_logger().error(f"Error verifying thread access: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Error verifying thread access: {str(e)}"
        )

async def get_user_id_from_stream_auth_new(
    request: Request,
    token: Optional[str] = None
) -> str:
    """
    Extract and verify the user ID from multiple authentication methods.
    This function is specifically designed for streaming endpoints that need to support both
    header-based and query parameter-based authentication (for EventSource compatibility).
    
    Supports authentication via:
    1. X-API-Key header (public:secret key pairs from API keys table) 
    2. Authorization header with Bearer token (new local JWT)
    3. Query parameter token (JWT for EventSource compatibility)
    
    Args:
        request: The FastAPI request object
        token: Optional JWT token from query parameters
        
    Returns:
        str: The user ID extracted from the authentication method
        
    Raises:
        HTTPException: If no valid token is found or if the token is invalid
    """
    try:
        # First, try the standard authentication (handles both API keys and Authorization header)
        try:
            return await get_current_user_id_from_jwt_new(request)
        except HTTPException:
            # If standard auth fails, try query parameter JWT for EventSource compatibility
            pass
        
        # Try to get user_id from token in query param (for EventSource which can't set headers)
        if token:
            try:
                # Use new JWT auth system
                token_data = JWTAuth.verify_access_token(token)
                user_id = token_data["user_id"]
                
                if user_id:
                    sentry.sentry.set_user({ "id": user_id })
                    structlog.contextvars.bind_contextvars(
                        user_id=user_id,
                        auth_method="local_jwt_query",
                        token_jti=token_data.get("jti")
                    )
                    return user_id
            except Exception:
                pass
        
        # If we still don't have a user_id, return authentication error
        raise HTTPException(
            status_code=401,
            detail="No valid authentication credentials found",
            headers={"WWW-Authenticate": "Bearer"}
        )
    except HTTPException:
        # Re-raise HTTP exceptions as they are
        raise
    except Exception as e:
        error_msg = str(e)
        if "cannot schedule new futures after shutdown" in error_msg or "connection is closed" in error_msg:
            raise HTTPException(
                status_code=503,
                detail="Server is shutting down"
            )
        else:
            raise HTTPException(
                status_code=500,
                detail=f"Error during authentication: {str(e)}"
            )

async def get_optional_user_id_new(request: Request) -> Optional[str]:
    """
    Extract the user ID from the JWT in the Authorization header if present,
    but don't require authentication. Returns None if no valid token is found.
    
    This function is used for endpoints that support both authenticated and 
    unauthenticated access (like public projects).
    
    Args:
        request: The FastAPI request object
        
    Returns:
        Optional[str]: The user ID extracted from the JWT, or None if no valid token
    """
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        return None
    
    token = auth_header.split(' ')[1]
    
    try:
        # Use new JWT auth system
        token_data = JWTAuth.verify_access_token(token)
        user_id = token_data["user_id"]
        
        if user_id:
            sentry.sentry.set_user({ "id": user_id })
            structlog.contextvars.bind_contextvars(
                user_id=user_id,
                auth_method="local_jwt_optional"
            )
        
        return user_id
    except Exception:
        return None

async def verify_admin_api_key(x_admin_api_key: Optional[str] = Header(None)):
    """
    Verify admin API key (unchanged from original)
    """
    expected_key = config.ADMIN_API_KEY if hasattr(config, 'ADMIN_API_KEY') else os.getenv('ADMIN_API_KEY')
    if not expected_key:
        raise HTTPException(status_code=500, detail="Admin API key not configured")
    
    if not x_admin_api_key or x_admin_api_key != expected_key:
        raise HTTPException(status_code=403, detail="Invalid admin API key")
    
    return True 