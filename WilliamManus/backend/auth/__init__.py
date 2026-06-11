"""
用户认证模块
专业的JWT认证系统，使用本地PostgreSQL
"""

from .api import router as auth_router
from .service import AuthService
from .models import (
    LoginRequest, RegisterRequest, RefreshRequest,
    AuthResponse, RefreshResponse, UserResponse, User
)

__all__ = [
    'auth_router',
    'AuthService',
    'LoginRequest', 'RegisterRequest', 'RefreshRequest',
    'AuthResponse', 'RefreshResponse', 'UserResponse', 'User'
] 