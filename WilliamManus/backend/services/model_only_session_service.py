"""
自定义ADK数据库会话服务 - 只存储模型响应，过滤用户消息
"""

from typing import Any, Optional
import logging

from google.adk.sessions.database_session_service import (  # type: ignore
    DatabaseSessionService,
    StorageSession,
    StorageEvent,
    StorageAppState,
    StorageUserState,
    GetSessionConfig,
    _merge_state,
)
from google.adk.sessions.session import Session  # type: ignore
from google.adk.events.event import Event  # type: ignore
from google.adk.events.event_actions import EventActions  # type: ignore

from sqlalchemy import select  # type: ignore
from datetime import datetime

from utils.db_url import to_sqlalchemy_async_url, sanitize_db_url

logger = logging.getLogger(__name__)

class ModelOnlyDBSessionService(DatabaseSessionService):
    """
    继承ADK的DatabaseSessionService，只存储模型响应事件
    过滤掉用户消息事件，避免与手动插入的用户消息重复
    """
    
    def __init__(self, db_url: str, **kwargs: Any):
        """初始化服务

        ADK uses SQLAlchemy asyncio, so ensure we pass an async dialect URL.
        """
        async_db_url = to_sqlalchemy_async_url(db_url)
        if async_db_url != db_url:
            logger.info(
                "Converting DATABASE_URL for ADK: raw=%s -> async=%s",
                sanitize_db_url(db_url),
                sanitize_db_url(async_db_url),
            )
        else:
            logger.info("Using DATABASE_URL for ADK: %s", sanitize_db_url(db_url))

        super().__init__(async_db_url, **kwargs)
        logger.info("ModelOnlyDBSessionService initialized - will filter user events")
    
    async def append_event(self, session: Session, event: Event) -> Event:
        """
        重写append_event方法，过滤用户事件
        只存储模型/助手的响应，避免用户消息重复
        """
        # 过滤用户事件，不存储到数据库，因为我们已经手动存储了
        if getattr(event, "author", None) == "user":
            logger.debug(f" Filtering user event: {event.id}")
            return event  # 直接返回，不调用父类存储方法
        
        # 存储非用户事件（模型响应等）
        logger.debug(f"Storing non-user event: {event.id} (author: {getattr(event, 'author', 'unknown')})")
        return await super().append_event(session, event) 

    async def get_session(  # type: ignore[override]
        self,
        *,
        app_name: str,
        user_id: str,
        session_id: str,
        config: Optional[GetSessionConfig] = None,
    ) -> Optional[Session]:
        """
        Override ADK get_session to be resilient to older/custom `actions` payloads.

        Older versions (and our prior manual inserts) stored `actions` as a pickled
        plain dict or even empty bytes. ADK>=1.12 expects a Pydantic EventActions
        instance and calls `.model_dump()` on it.

        Here we normalize any non-EventActions value into EventActions so session
        loading never crashes and fallback isn't triggered.
        """
        await self._ensure_tables_created()

        async with self.database_session_factory() as sql_session:
            storage_session = await sql_session.get(
                StorageSession, (app_name, user_id, session_id)
            )
            if storage_session is None:
                return None

            stmt = (
                select(StorageEvent)
                .filter(StorageEvent.app_name == app_name)
                .filter(StorageEvent.session_id == storage_session.id)
                .filter(StorageEvent.user_id == user_id)
            )

            if config and getattr(config, "after_timestamp", None):
                after_dt = datetime.fromtimestamp(config.after_timestamp)
                stmt = stmt.filter(StorageEvent.timestamp >= after_dt)

            stmt = stmt.order_by(StorageEvent.timestamp.desc())

            if config and getattr(config, "num_recent_events", None):
                stmt = stmt.limit(config.num_recent_events)

            result = await sql_session.execute(stmt)
            storage_events = result.scalars().all()

            # Normalize actions for backward compatibility.
            for e in storage_events:
                actions_val = getattr(e, "actions", None)
                if actions_val is None or not hasattr(actions_val, "model_dump"):
                    if isinstance(actions_val, dict):
                        try:
                            e.actions = EventActions.model_validate(actions_val)
                        except Exception:
                            e.actions = EventActions()
                    else:
                        e.actions = EventActions()

            storage_app_state = await sql_session.get(StorageAppState, (app_name))
            storage_user_state = await sql_session.get(
                StorageUserState, (app_name, user_id)
            )

            app_state = storage_app_state.state if storage_app_state else {}
            user_state = storage_user_state.state if storage_user_state else {}
            session_state = storage_session.state

            merged_state = _merge_state(app_state, user_state, session_state)

            events = [e.to_event() for e in reversed(storage_events)]
            session = storage_session.to_session(state=merged_state, events=events)

        return session
