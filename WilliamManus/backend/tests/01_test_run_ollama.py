from google.adk.agents import Agent
from google.adk.models.lite_llm import LiteLlm
import os
import asyncio
from dotenv import load_dotenv
from google.genai import types
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService


APP_NAME = "chatbot"  # 手动定义应用名称
USER_ID = "user_1"    # 手动定义用户ID
SESSION_ID = "session_001"  # 手动定义会话ID

async def main():
    # 1. 通过 litellm 接入 DeepSeek 模型
    model = LiteLlm(
        model="ollama_chat/qwen3:14b",  # 替换为你的 Ollama 模型
        base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
    )

    # 2. 使用最简模式构建一个Agent代理
    init_agent = Agent(
        name="chatbot",
        model=model,
        instruction="你是一个乐于助人的中文助手。",
    )



    # 3. 创建内存会话缓存
    session_service = InMemorySessionService()

    # 4. 在内存中实际创建缓存数据
    await session_service.create_session(app_name=APP_NAME, user_id=USER_ID, session_id=SESSION_ID)

    # 5. 创建Runner实例对象
    runner = Runner(
        app_name="chatbot",
        agent=init_agent,
        session_service=session_service
    )



    query = "你好，请你介绍一下你自己。"

    # 将用户的问题转换为 ADK 框架的兼容格式
    content = types.Content(
        role='user', 
        parts=[types.Part(text=query)]
        )

    # 异步运行
    async for event in runner.run_async(
        user_id="user_1",
        session_id="session_001", 
        new_message=content,
    ):
        # 处理事件
        print(event)

if __name__ == "__main__":
    asyncio.run(main())
