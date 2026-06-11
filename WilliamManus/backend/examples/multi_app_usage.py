#!/usr/bin/env python3
"""
多应用架构使用示例
展示如何支持用户创建多个agent应用
"""

import asyncio
from auth.service import AuthService
from utils.logger import logger


async def example_multi_app_usage():
    """多应用架构使用示例"""
    
    auth_service = AuthService()
    
    # 模拟用户ID
    user_id = "user_123"
    
    print("=== 多应用架构示例 ===\n")
    
    # 1. 用户注册/登录（使用默认应用）
    print("1. 用户注册/登录")
    print(f"   默认应用: {auth_service.default_app_name}")
    print("   - 用户认证相关的会话和事件都存储在默认应用中")
    print("   - app_name: fufanmanus")
    print("   - 包含: 登录事件、注册事件、用户状态等\n")
    
    # 2. 用户创建第一个agent
    print("2. 用户创建第一个agent")
    agent1_id = "agent_chatbot_001"
    agent1_config = {
        "name": "智能客服",
        "type": "chatbot",
        "model": "gpt-4",
        "description": "专业的客户服务助手"
    }
    
    session1_id = await auth_service.create_agent_session(
        user_id, agent1_id, agent1_config
    )
    print(f"   Agent ID: {agent1_id}")
    print(f"   Session ID: {session1_id}")
    print(f"   App Name: agent_{agent1_id}")
    print("   - 每个agent都有独立的app_name")
    print("   - 会话和事件完全隔离\n")
    
    # 3. 用户创建第二个agent
    print("3. 用户创建第二个agent")
    agent2_id = "agent_analyzer_002"
    agent2_config = {
        "name": "数据分析师",
        "type": "analyzer",
        "model": "claude-3",
        "description": "专业的数据分析助手"
    }
    
    session2_id = await auth_service.create_agent_session(
        user_id, agent2_id, agent2_config
    )
    print(f"   Agent ID: {agent2_id}")
    print(f"   Session ID: {session2_id}")
    print(f"   App Name: agent_{agent2_id}")
    print("   - 不同的agent有不同的app_name")
    print("   - 数据完全隔离，互不影响\n")
    
    # 4. 获取用户的所有agents
    print("4. 获取用户的所有agents")
    agents = await auth_service.get_user_agents(user_id)
    print(f"   用户 {user_id} 创建的agents:")
    for agent in agents:
        print(f"   - {agent['agent_id']}: {agent['config']['name']}")
        print(f"     会话ID: {agent['session_id']}")
        print(f"     创建时间: {agent['created_at']}")
    
    print("\n=== 架构优势 ===")
    print("✅ 每个agent独立的应用空间")
    print("✅ 会话和事件完全隔离")
    print("✅ 支持用户创建无限个agent")
    print("✅ 符合ADK框架的设计理念")
    print("✅ 便于后续扩展和管理")


if __name__ == "__main__":
    asyncio.run(example_multi_app_usage()) 