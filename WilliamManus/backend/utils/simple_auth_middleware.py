"""
用户认证中间件
用于保护现有的API端点
"""

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from auth.access_policy import require_roys_alpha_access
from utils.auth_utils import AuthUtils
from utils.logger import logger, structlog

security = HTTPBearer(auto_error=False)
auth_utils = AuthUtils()

async def get_current_user_id_from_jwt(
    request: Request
) -> str:
    """
    从JWT token中提取用户ID
    这个函数替代原来的 get_current_user_id_from_jwt，保持接口兼容
    """
    # 对于OPTIONS请求，跳过认证检查
    if request.method == "OPTIONS":
        return "anonymous"  # 返回一个占位符，不会被使用
        
    # 检查Authorization头
    auth_header = request.headers.get('Authorization')
    
    if not auth_header or not auth_header.startswith('Bearer '):
        raise HTTPException(
            status_code=401,
            detail="No valid authentication credentials found",
            headers={"WWW-Authenticate": "Bearer"}
        )
    
    token = auth_header.split(' ')[1]
    
    try:
        token_data = auth_utils.verify_token(token)
        user_id = token_data["user_id"]
        structlog.contextvars.bind_contextvars(user_id=user_id)
        await require_roys_alpha_access(user_id)
        
        logger.debug(f"Authenticated user: {user_id}")
        return user_id
        
    except HTTPException:
        raise
    except Exception as e:
        logger.warning(f"JWT verification failed: {type(e).__name__}")
        raise HTTPException(
            status_code=401,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"}
        )

async def get_user_id_from_stream_auth(
    request: Request,
    token: Optional[str] = None
) -> str:
    """
    支持流式端点的认证
    """
    try:
        # 首先尝试标准认证
        return await get_current_user_id_from_jwt(request)
    except HTTPException:
        pass
    
    # 尝试从查询参数获取token（用于EventSource）
    if token:
        try:
            token_data = auth_utils.verify_token(token)
            user_id = token_data["user_id"]
            structlog.contextvars.bind_contextvars(user_id=user_id)
            await require_roys_alpha_access(user_id)
            return user_id
        except Exception:
            pass
    
    raise HTTPException(
        status_code=401,
        detail="No valid authentication credentials found",
        headers={"WWW-Authenticate": "Bearer"}
    )

async def get_optional_user_id(request: Request) -> Optional[str]:
    """
    可选的用户认证，不强制要求认证
    """
    try:
        return await get_current_user_id_from_jwt(request)
    except HTTPException:
        return None

# 为了兼容现有代码，保持相同的函数名
async def verify_thread_access(client, thread_id: str, user_id: str):
    """
    验证用户对线程的访问权限
    """
    try:
        # 查询线程的完整信息
        thread_result = await client.table('threads').select('*').eq('thread_id', thread_id).execute()
        
        if not thread_result.data:
            raise HTTPException(status_code=404, detail="Thread not found")
        
        thread_data = thread_result.data[0]
        thread_user_id = thread_data['account_id']
        
        # 1. 检查是否为线程所有者
        if thread_user_id == user_id:
            return True
        
        # # 2. 检查项目是否为公开项目 TODO：如果涉及项目公开需求，可以放开，示例：
        # project_id = thread_data.get('project_id')
        # if project_id:
        #     project_result = await client.table('projects').select('is_public').eq('project_id', project_id).execute()
        #     if project_result.data and len(project_result.data) > 0:
        #         if project_result.data[0].get('is_public'):
        #             return True
        
        # 3. 检查是否为账户成员（如果需要团队协作功能）
        # 这里可以根据你的具体需求实现账户成员检查
        # 例如：检查用户是否在同一个账户/团队中
        
        # 如果都不满足，则拒绝访问
        raise HTTPException(status_code=403, detail="Not authorized to access this thread")
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error verifying thread access: {str(e)}")
        raise HTTPException(
            status_code=500, 
            detail=f"Error verifying thread access: {str(e)}"
        ) 
