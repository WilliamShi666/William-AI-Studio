#!/usr/bin/env python3
"""
执行fufanmanus.sql迁移文件的脚本
创建所有业务表：用户认证、项目管理、代理系统、ADK框架
"""

import asyncio
import sys
import os
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.postgresql import DBConnection
from utils.logger import logger


async def execute_fufanmanus_migration():
    """执行fufanmanus.sql迁移"""
    db = None
    try:
        logger.info("开始执行fufanmanus.sql迁移...")
        
        # 初始化数据库连接
        db = DBConnection()
        await db.initialize()
        client = await db.client
        
        # 读取迁移文件
        migration_file = project_root / "migrations" / "fufanmanus.sql"
        if not migration_file.exists():
            logger.error(f"迁移文件不存在: {migration_file}")
            return False
        
        with open(migration_file, 'r', encoding='utf-8') as f:
            migration_sql = f.read()
        
        logger.info(f"读取迁移文件: {migration_file}")
        
        # 执行迁移
        async with client.pool.acquire() as conn:
            await conn.execute(migration_sql)
        
        logger.info("fufanmanus.sql迁移完成！")
        
        # 验证表是否创建成功
        async with client.pool.acquire() as conn:
            tables = await conn.fetch(
                """
                SELECT tablename 
                FROM pg_tables 
                WHERE schemaname = 'public' 
                ORDER BY tablename
                """
            )
        
        logger.info(f"数据库中共有 {len(tables)} 个表:")
        for table in tables:
            logger.info(f"  - {table['tablename']}")
        
        # 验证关键表是否存在
        expected_tables = [
            'users', 'agents', 'projects', 'messages', 'threads',
            'sessions', 'events', 'app_states', 'user_states'
        ]
        
        existing_table_names = [table['tablename'] for table in tables]
        missing_tables = [table for table in expected_tables if table not in existing_table_names]
        
        if missing_tables:
            logger.warning(f"以下关键表未找到: {missing_tables}")
        else:
            logger.info("所有关键表都已创建成功！")
        
        return True
        
    except Exception as e:
        logger.error(f"fufanmanus.sql迁移失败: {e}")
        return False
    finally:
        if db:
            await DBConnection.disconnect()


def main():
    """主函数"""
    print("FuFanManus 数据库表迁移工具")
    print("=" * 50)
    
    success = asyncio.run(execute_fufanmanus_migration())
    if success:
        print("\n数据库表创建成功！")
        print("\n已创建的16个核心表：")
        print("用户认证: users, oauth_providers, user_sessions, refresh_tokens, user_activities")
        print("项目管理: projects, threads, messages")
        print("代理系统: agents, agent_versions, agent_workflows, agent_runs")
        print("ADK框架: app_states, sessions, events, user_states")
        print("\n🚀 现在可以启动服务了: python -m uvicorn api:app --reload")
    else:
        print("\n数据库表创建失败！")
        print("请检查：")
        print("1. 数据库连接是否正常")
        print("2. .env 文件中的 DATABASE_URL 是否正确")
        print("3. migrations/fufanmanus.sql 文件是否存在")
        sys.exit(1)


if __name__ == "__main__":
    main() 