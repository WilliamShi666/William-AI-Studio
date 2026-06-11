import asyncio
import base64
import os
import time
import re
from dotenv import load_dotenv
from urllib.parse import urlparse

load_dotenv()

from browser_use import Agent, BrowserSession
from browser_use.llm import ChatOpenAI
from e2b_code_interpreter import Sandbox

# 使用配置系统
from utils.config import Configuration
config = Configuration()

def save_screenshots_from_history(history, session_id: str):
    """根据官方文档保存截图的方法"""
    print(f"\n🖼️  正在保存截图 (会话ID: {session_id})...")
    
    try:
        # 创建截图目录
        screenshots_dir = os.path.join(".", "screenshots", str(session_id))
        os.makedirs(screenshots_dir, exist_ok=True)
        
        saved_screenshots = []
        
        # 方法A: 使用 history.screenshots() 获取base64截图
        screenshots_b64 = history.screenshots()
        print(f"📸 找到 {len(screenshots_b64)} 个base64截图")
        
        for i, b64img in enumerate(screenshots_b64):
            try:
                # 解码base64
                img_data = base64.b64decode(b64img)
                
                # 生成文件名
                screenshot_path = os.path.join(screenshots_dir, f"screenshot_{i:02d}_{time.time():.0f}.png")
                
                # 保存文件
                with open(screenshot_path, "wb") as f:
                    f.write(img_data)
                
                saved_screenshots.append(screenshot_path)
                print(f"✅ 截图 {i+1} 已保存: {screenshot_path}")
                
            except Exception as e:
                print(f"❌ 保存截图 {i+1} 失败: {e}")
        
        # 方法B: 使用 history.screenshot_paths() 获取临时文件路径
        temp_paths = history.screenshot_paths()
        print(f"📁 找到 {len(temp_paths)} 个临时截图文件")
        
        for i, temp_path in enumerate(temp_paths):
            try:
                if os.path.exists(temp_path):
                    # 复制到我们的目录
                    final_path = os.path.join(screenshots_dir, f"temp_screenshot_{i:02d}_{time.time():.0f}.png")
                    
                    # 读取临时文件并复制
                    with open(temp_path, "rb") as src:
                        with open(final_path, "wb") as dst:
                            dst.write(src.read())
                    
                    saved_screenshots.append(final_path)
                    print(f"✅ 临时截图 {i+1} 已复制: {final_path}")
                else:
                    print(f"⚠️  临时文件不存在: {temp_path}")
                    
            except Exception as e:
                print(f"❌ 复制临时截图 {i+1} 失败: {e}")
        
        print(f"🎉 截图保存完成! 共保存 {len(saved_screenshots)} 个文件")
        return saved_screenshots
        
    except Exception as e:
        print(f"❌ 截图保存过程出错: {e}")
        import traceback
        traceback.print_exc()
        return []

async def main():
    print("🚀 启动 browser-use 官方截图方案...")
    print(f"E2B API Key: {os.getenv('E2B_API_KEY')[:20]}...")
    print(f"E2B Domain: {os.getenv('E2B_DOMAIN')}")
    
    sandbox = Sandbox(
        timeout=600,
        template="browser-chromium",
    )
    
    try:
        # 获取Chrome调试地址
        host = sandbox.get_host(9223)
        cdp_url = f"https://{host}"
        print(f"Chrome CDP URL: {cdp_url}")
        
        # 创建浏览器会话
        browser_session = BrowserSession(cdp_url=cdp_url)
        await browser_session.start()
        print("✅ BrowserSession 启动成功")

        # 创建Agent（使用你的固定LLM设置）
        # Note: Replace 'YOUR_OPENAI_API_KEY' with your actual API key
        agent = Agent(
            task="去百度搜索 Browser-use 的相关信息，并总结出 3 个使用场景。请在每个重要页面都停留一下，方便截图。",
            llm=ChatOpenAI(
                api_key='YOUR_OPENAI_API_KEY',  # Replace with your actual API key or use environment variable
                base_url='https://ai.devtool.tech/proxy/v1',
                model="gpt-4o",
                temperature=0.7
            ),
            browser_session=browser_session,
            use_vision=False,  # 避免兼容性问题
        )
        
        print("🤖 开始执行Agent任务...")
        
        # 运行Agent并获取历史记录
        history = await agent.run(max_steps=10)
        
        print(f"✅ Agent任务完成!")
        print(f"📊 执行步骤: {history.number_of_steps()}")
        print(f"⏱️  总用时: {history.total_duration_seconds():.2f} 秒")
        print(f"🌐 访问的URLs: {history.urls()}")
        
        # 检查是否成功
        if history.is_successful():
            print("🎯 任务执行成功!")
        else:
            print("⚠️  任务可能未完全成功")
        
        # 保存截图（核心功能！）
        screenshots = save_screenshots_from_history(history, browser_session.id)
        
        # 显示最终结果
        final_result = history.final_result()
        if final_result:
            print(f"\n📋 最终结果: {final_result}")
        
        # 关闭浏览器会话
        await browser_session.stop()
        print("✅ BrowserSession 已停止")
        
        return screenshots
        
    except Exception as e:
        print(f"❌ 主程序出错: {e}")
        import traceback
        traceback.print_exc()
        return []
        
    finally:
        # 清理沙箱
        sandbox.kill()
        print("🧹 沙箱已清理")

if __name__ == "__main__":
    screenshots = asyncio.run(main())
    print(f"\n🏁 程序完成! 保存了 {len(screenshots)} 个截图文件")
    for path in screenshots:
        print(f"   📸 {path}") 