"""
User Authentication Service
"""

import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import HTTPException # type: ignore
from services.postgresql import DBConnection
from utils.auth_utils import AuthUtils
from utils.logger import logger
from .access_policy import build_user_capabilities, user_has_roys_alpha_access
from .models import (
    LoginRequest, RegisterRequest, RefreshRequest,
    AuthResponse, RefreshResponse, UserResponse, User
)

class AuthService:
    """User Authentication Service"""
    
    def __init__(self):
        self.db = DBConnection()
        self.auth = AuthUtils()
        self.default_app_name = "fufanmanus"  # 默认应用名称
    
    async def _get_client(self):
        """获取数据库客户端"""
        await self.db.initialize()
        return await self.db.client
    
    def _user_to_model(self, user_data: dict) -> User:
        """转换数据库用户数据为模型"""
        data = user_data if isinstance(user_data, dict) else dict(user_data)
        return User(
            id=str(data['id']),  # 确保UUID转换为字符串
            email=data['email'],
            name=data['name'],
            role=data.get('role', 'user'),
            created_at=data['created_at'],
            capabilities=build_user_capabilities(data),
        )

    def _get_access_token_expires_at(self) -> int:
        expire_at = datetime.now(timezone.utc) + timedelta(
            hours=AuthUtils.ACCESS_TOKEN_EXPIRE_HOURS
        )
        return int(expire_at.timestamp())

    def _create_access_token_for_user(self, user_data: dict) -> str:
        return self.auth.create_access_token(
            str(user_data["id"]),
            user_data.get("role", "user"),
            can_access_roys_alpha=user_has_roys_alpha_access(user_data),
        )
    
    async def register(self, request: RegisterRequest) -> AuthResponse:
        """User Registration"""
        logger.info(f"Starting registration for: {request.email}")
        
        client = await self._get_client()
        
        # 检查用户是否已存在
        async with client.pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id FROM users WHERE email = $1", 
                request.email
            )
        
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered")
        
        # 创建新用户
        hashed_password = self.auth.hash_password(request.password)
        
        async with client.pool.acquire() as conn:
            user_record = await conn.fetchrow(
                """
                INSERT INTO users (email, password_hash, name, provider, status, role, created_at) 
                VALUES ($1, $2, $3, $4, $5, $6, $7) 
                RETURNING *
                """,
                request.email, hashed_password, request.name, 'local', 'active', 'user', datetime.now()
            )
        
        user = dict(user_record)
        
        # 生成访问令牌和刷新令牌
        access_token = self._create_access_token_for_user(user)
        refresh_token = self.auth.create_refresh_token()
        
        # 存储刷新令牌
        await self._store_refresh_token(str(user['id']), refresh_token)
        
        # 更新用户状态
        await self._update_user_state(client, str(user['id']), {
            'last_login': datetime.now().isoformat(),
            'login_count': 1,
            'email_verified': False
        })
        
        # 使用默认应用名称
        app_name = self.default_app_name
        
        # 创建注册会话（注册时先创建会话，再记录事件，用来适配ADK框架中无法使用session_id字段的情况）
        await self._create_adk_session(client, str(user['id']), {
            'registration_time': datetime.now().isoformat(),
            'registration_method': 'email_password',
            'status': 'active'
        }, app_name)
        
        # 注册时创建FuFanManus Agent
        from agent.fufanmanus.repository import FufanmanusAgentRepository
        repository = FufanmanusAgentRepository()
        await repository.create_fufanmanus_agent(str(user['id']))
        
        logger.info(f"User registered: {request.email}")
        
        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=24 * 3600,  # 24小时
            expires_at=self._get_access_token_expires_at(),
            user=self._user_to_model(user)
        )
    
    async def login(self, request: LoginRequest) -> AuthResponse:
        """User login"""
        client = await self._get_client()
        
        # 查找用户（包含ADK状态字段）
        async with client.pool.acquire() as conn:
            user = await conn.fetchrow(
                """
                SELECT id, email, name, password_hash, provider, status, 
                       role, email_verified, created_at, last_login_at
                FROM users 
                WHERE email = $1 AND provider = 'local'
                """,
                request.email
            )
        if not user:
            raise HTTPException(status_code=401, detail="Invalid email or password")
        user = dict(user)
        
        # 检查用户状态
        if user['status'] != 'active':
            raise HTTPException(status_code=401, detail="Account is not active")
        
        # 验证密码
        if not self.auth.verify_password(request.password, user['password_hash']):
            raise HTTPException(status_code=401, detail="Invalid email or password")
        
        # 登录前先清除该用户的所有旧refresh tokens
        async with client.pool.acquire() as conn:
            old_tokens_result = await conn.execute(
                "DELETE FROM refresh_tokens WHERE user_id = $1",
                str(user['id'])
            )
        old_tokens_count = int(old_tokens_result.split()[-1]) if old_tokens_result else 0
        logger.info(f"Login: Cleared {old_tokens_count} old refresh tokens for user {user['id']}")
        
        # 生成新的tokens
        access_token = self._create_access_token_for_user(user)
        refresh_token = self.auth.create_refresh_token()
        
        # 存储新的刷新token
        await self._store_refresh_token(str(user['id']), refresh_token)
        
        # 更新最后登录时间（ADK兼容）
        async with client.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_login_at = $1 WHERE id = $2",
                datetime.now(), user['id']
            )
        
        # 使用默认应用名称
        app_name = self.default_app_name
        
        # 创建或更新ADK用户状态
        await self._update_user_state(client, str(user['id']), {
            'last_login': datetime.now().isoformat(),
            'login_method': 'email_password',
            'status': 'active'
        }, app_name)
        
        # 创建ADK会话
        session_id = await self._create_adk_session(client, str(user['id']), {
            'access_token': access_token,
            'login_time': datetime.now().isoformat(),
            'login_method': 'email_password'
        }, app_name)
        
        logger.info(f"User logged in: {request.email}")
        
        return AuthResponse(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=24 * 3600,  # 24小时
            expires_at=self._get_access_token_expires_at(),
            user=self._user_to_model(user)
        )
    
    async def refresh_token(self, request: RefreshRequest) -> RefreshResponse:
        """刷新token"""
        client = await self._get_client()
        
        # 验证刷新token
        token_hash = self.auth.hash_refresh_token(request.refresh_token)
        async with client.pool.acquire() as conn:
            result = await conn.fetchrow(
                """
                SELECT user_id FROM refresh_tokens 
                WHERE token_hash = $1 AND expires_at > $2
                """,
                token_hash, datetime.now()
            )
        
        if not result:
            raise HTTPException(status_code=401, detail="Invalid refresh token")
        
        user_id = result['user_id']
        user_id_str = str(user_id)
        
        # 删除旧的刷新token
        async with client.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM refresh_tokens WHERE token_hash = $1",
                token_hash
            )
        
        # 生成新的tokens
        user_record = None
        async with client.pool.acquire() as conn:
            user_record = await conn.fetchrow(
                "SELECT id, email, role, status FROM users WHERE id = $1",
                user_id
            )
        if not user_record:
            raise HTTPException(status_code=401, detail="User not found")

        user_data = dict(user_record)
        user_data["id"] = str(user_data.get("id"))

        access_token = self._create_access_token_for_user(user_data)
        new_refresh_token = self.auth.create_refresh_token()
        
        # 存储新的刷新token
        await self._store_refresh_token(user_id_str, new_refresh_token)
        
        return RefreshResponse(
            access_token=access_token,
            refresh_token=new_refresh_token,
            expires_in=24 * 3600,  # 24小时
            expires_at=self._get_access_token_expires_at(),
        )
    
    async def get_user(self, user_id: str) -> UserResponse:
        """Get user info"""
        client = await self._get_client()
        
        async with client.pool.acquire() as conn:
            user = await conn.fetchrow(
                "SELECT * FROM users WHERE id = $1",
                user_id
            )
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        return UserResponse(user=self._user_to_model(user))
    
    async def logout(self, user_id: str, refresh_token: Optional[str] = None):
        """Logout user"""
        client = await self._get_client()
        
        # 1. 删除refresh tokens (建议删除所有，避免token不一致问题)
        async with client.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM refresh_tokens WHERE user_id = $1",
                user_id
            )
        
        # 提取删除的行数
        deleted_count = int(result.split()[-1]) if result else 0
        logger.info(f"Deleted {deleted_count} refresh tokens for user {user_id}")
        
        # 2. 更新用户状态为 'offline'
        await self._update_user_state(client, user_id, {
            'status': 'offline',
            'logout_time': datetime.now().isoformat(),
            'last_activity': datetime.now().isoformat()
        }, self.default_app_name)
        
        # 3. 关闭用户的所有活跃ADK sessions
        await self._close_user_sessions(client, user_id, self.default_app_name)
        
        # 4. 记录logout事件到ADK
        await self._log_logout_event(client, user_id, self.default_app_name)
        
        logger.info(f"User logged out completely: {user_id}")
    
    async def _store_refresh_token(self, user_id: str, refresh_token: str):
        """存储刷新token"""
        client = await self._get_client()
        
        token_hash = self.auth.hash_refresh_token(refresh_token)
        expires_at = self.auth.get_refresh_token_expire_time()
        
        async with client.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO refresh_tokens (user_id, token_hash, expires_at, created_at)
                VALUES ($1, $2, $3, $4)
                """,
                user_id, token_hash, expires_at, datetime.now()
            )
    
    async def _update_user_state(self, client, user_id: str, state_data: dict, app_name: str = "fufanmanus"):
        """Update user state"""
        try:
            # 检查是否已存在用户状态
            async with client.pool.acquire() as conn:
                existing = await conn.fetchrow(
                    "SELECT * FROM user_states WHERE app_name = $1 AND user_id = $2",
                    app_name, user_id
                )
            
            if existing:
                # 更新现有状态
                try:
                    # 解析现有的JSON状态
                    current_state = json.loads(existing['state']) if existing['state'] else {}
                except (json.JSONDecodeError, TypeError):
                    # 如果解析失败，使用空字典
                    current_state = {}
                
                # 更新状态
                current_state.update(state_data)
                
                async with client.pool.acquire() as conn:
                    await conn.execute(
                        """
                        UPDATE user_states 
                        SET state = $1, update_time = $2 
                        WHERE app_name = $3 AND user_id = $4
                        """,
                        json.dumps(current_state), datetime.now(), app_name, user_id
                    )
            else:
                # 创建新的用户状态
                async with client.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO user_states (app_name, user_id, state, update_time)
                        VALUES ($1, $2, $3, $4)
                        """,
                        app_name, user_id, json.dumps(state_data), datetime.now()
                    )
            
            logger.info(f"Updated user state for {user_id} in {app_name}")
            
        except Exception as e:
            logger.warning(f"Failed to update user state: {e}")
    
    async def _create_adk_session(self, client, user_id: str, session_data: dict, app_name: str = "fufanmanus"):
        """Create ADK session"""
        try:
            # 生成会话ID
            import uuid
            session_id = str(uuid.uuid4())
            
            # 按照ADK框架的表结构插入会话
            async with client.pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO sessions (app_name, user_id, id, state, create_time, update_time)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    """,
                    app_name, user_id, session_id, json.dumps(session_data), datetime.now(), datetime.now()
                )
            
            logger.info(f"Created ADK session {session_id} for user {user_id} in app {app_name}")
            return session_id
            
        except Exception as e:
            logger.warning(f"Failed to create ADK session: {e}")
            return None
    
    async def _log_adk_event(self, client, user_id: str, event_type: str, event_data: dict, 
                           session_id: str = None, app_name: str = "fufanmanus"):
        """记录Google ADK事件"""
        try:
            import uuid
            import pickle
            from google.adk.events.event_actions import EventActions  # type: ignore
            event_id = str(uuid.uuid4())
            invocation_id = str(uuid.uuid4())
            
            if session_id:
                # 按照ADK框架的表结构插入事件
                async with client.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO events (
                            id, app_name, user_id, session_id, invocation_id, 
                            author, timestamp, content, actions
                            )
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                        """,
                        event_id, app_name, user_id, session_id, invocation_id,
                        "auth_service", datetime.now(), json.dumps(event_data), pickle.dumps(EventActions())
                    )
                
                logger.info(f"Logged ADK event {event_type} for user {user_id} with session {session_id}")
            else:
                logger.warning(f"Failed to create session for event {event_type}, skipping event log")
            
        except Exception as e:
            logger.warning(f"Failed to log ADK event: {e}") 

    async def _get_app_name_for_user(self, user_id: str, context: str = "default") -> str:
        """获取用户的应用名称
        
        Args:
            user_id: 用户ID
            context: 上下文，可以是 'default', 'agent_creation', 'specific_agent' 等
            
        Returns:
            app_name: 应用名称
        """
        # 这里可以根据用户ID和上下文动态确定app_name
        # 例如：从数据库查询用户创建的应用，或者根据请求上下文确定
        
        if context == "default":
            return self.default_app_name
        elif context == "agent_creation":
            # 用户创建新agent时，可以使用特定的app_name
            return f"agent_creator_{user_id}"
        else:
            # 其他情况返回默认应用
            return self.default_app_name
    
    async def create_agent_session(self, user_id: str, agent_id: str, agent_config: dict) -> str:
        """为用户创建特定agent的会话
        
        Args:
            user_id: 用户ID
            agent_id: Agent ID
            agent_config: Agent配置
            
        Returns:
            session_id: 会话ID
        """
        client = await self._get_client()
        
        # 为特定agent创建app_name
        app_name = f"agent_{agent_id}"
        
        # 创建agent会话
        session_id = await self._create_adk_session(client, user_id, {
            'agent_id': agent_id,
            'agent_config': agent_config,
            'created_at': datetime.now().isoformat(),
            'session_type': 'agent_session'
        }, app_name)
        
        # 记录agent创建事件
        if session_id:
            await self._log_adk_event(client, user_id, 'agent_session_created', {
                'agent_id': agent_id,
                'agent_config': agent_config,
                'created_at': datetime.now().isoformat()
            }, session_id, app_name)
        
        return session_id
    
    async def get_user_agents(self, user_id: str) -> list:
        """获取用户创建的所有agents
        
        Args:
            user_id: 用户ID
            
        Returns:
            agents: Agent列表
        """
        client = await self._get_client()
        
        # 查询用户的所有agent会话
        async with client.pool.acquire() as conn:
            sessions = await conn.fetch(
                """
                SELECT DISTINCT app_name, id as session_id, state
                FROM sessions 
                WHERE user_id = $1 AND app_name LIKE 'agent_%'
                ORDER BY create_time DESC
                """,
                user_id
            )
        
        agents = []
        for session in sessions:
            try:
                state = json.loads(session['state']) if session['state'] else {}
                agent_id = state.get('agent_id')
                if agent_id:
                    agents.append({
                        'agent_id': agent_id,
                        'session_id': session['session_id'],
                        'config': state.get('agent_config', {}),
                        'created_at': state.get('created_at')
                    })
            except Exception as e:
                logger.warning(f"Failed to parse agent session state: {e}")
        
        return agents

    async def _close_user_sessions(self, client, user_id: str, app_name: str = "fufanmanus"):
        """Close all active ADK sessions for a user"""
        try:
            # 更新所有该用户的sessions状态为closed
            async with client.pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE sessions 
                    SET state = jsonb_set(COALESCE(state, '{}'), '{status}', '"closed"'),
                        update_time = $1
                    WHERE app_name = $2 AND user_id = $3 
                    AND (state->>'status' IS NULL OR state->>'status' != 'closed')
                    """,
                    datetime.now(), app_name, user_id
                )
                
            closed_count = int(result.split()[-1]) if result else 0
            logger.info(f"Closed {closed_count} ADK sessions for user {user_id}")
            
        except Exception as e:
            logger.warning(f"Failed to close user sessions: {e}")
    
    async def _log_logout_event(self, client, user_id: str, app_name: str = "fufanmanus"):
        """Log logout event to ADK"""
        try:
            # 尝试找到最近的活跃session来记录logout事件
            async with client.pool.acquire() as conn:
                recent_session = await conn.fetchrow(
                    """
                    SELECT id FROM sessions 
                    WHERE app_name = $1 AND user_id = $2 
                    ORDER BY update_time DESC 
                    LIMIT 1
                    """,
                    app_name, user_id
                )
            
            session_id = recent_session['id'] if recent_session else None
            
            logout_data = {
                'event_type': 'user_logout',
                'logout_time': datetime.now().isoformat(),
                'reason': 'user_initiated'
            }
            
            await self._log_adk_event(client, user_id, "logout", logout_data, 
                                    session_id, app_name)
                                    
        except Exception as e:
            logger.warning(f"Failed to log logout event: {e}")
