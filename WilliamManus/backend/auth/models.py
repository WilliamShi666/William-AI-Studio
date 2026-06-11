"""
用户认证模型
"""

from pydantic import BaseModel
from typing import Optional
from datetime import datetime

# 请求模型
class LoginRequest(BaseModel):
    email: str
    password: str

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str

class RefreshRequest(BaseModel):
    refresh_token: str

# 响应模型
class UserCapabilities(BaseModel):
    can_access_roys_alpha: bool


class User(BaseModel):
    id: str
    email: str
    name: str
    role: str
    created_at: datetime
    capabilities: UserCapabilities

class AuthResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int  # 秒数
    expires_at: int
    user: User

class RefreshResponse(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int
    expires_at: int

class UserResponse(BaseModel):
    user: User 
