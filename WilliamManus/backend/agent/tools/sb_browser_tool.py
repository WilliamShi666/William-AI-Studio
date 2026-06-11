"""
Browser(cdp_url)	连接到 Chrome	_ensure_browser()
browser.start()	启动浏览器控制	_ensure_browser()
browser.new_page(url)	创建新页面并导航	browser_navigate_to()
page.act(instruction)	AI 驱动的页面操作	browser_click_element()
page.get_elements_by_css_selector()	CSS 选择器查找元素	browser_input_text()
element.fill(text)	填充输入框	browser_input_text()
page.press(keys)	发送按键	browser_send_keys()
page.screenshot()	截图	_take_screenshot_and_save()
page.get_url()	获取当前 URL	_get_page_info()
page.get_title()	获取页面标题	_get_page_info()
page.evaluate(js)	执行 JavaScript	browser_click_element(), browser_scroll_down()
browser.stop()	停止浏览器	__aexit__()
"""

from __future__ import annotations

import traceback
import json
import base64
import io
import os
import time
import asyncio
from typing import Optional, TYPE_CHECKING
from datetime import datetime
from PIL import Image

# 使用 Actor API 而不是 Agent API
from browser_use import Browser

from agentpress.tool import ToolResult
from sandbox.tool_base import SandboxToolsBase
from utils.logger import logger

if TYPE_CHECKING:
    from agentpress.adk_thread_manager import ADKThreadManager


class SandboxBrowserTool(SandboxToolsBase):
    """Tool for executing browser tasks using E2B browser-use Actor API."""
    
    def __init__(
        self,
        project_id: str,
        thread_manager: Optional["ADKThreadManager"] = None,
        *,
        shadow_clone_run_id: Optional[str] = None,
        strict_sandbox: bool = False,
    ):
        super().__init__(
            project_id=project_id,
            thread_manager=thread_manager,
            sandbox_type='browser',
            shadow_clone_run_id=shadow_clone_run_id,
            strict_sandbox=strict_sandbox,
        )
        self.browser = None
        self.current_page = None
        self._browser_initialized = False

    async def _ensure_browser(self):
        """Ensure browser is initialized using Actor API.

        确保浏览器已初始化，使用Actor API进行管理。
        此函数会检查浏览器是否已初始化，如果没有则创建新的浏览器实例。
        """
        # 如果浏览器已经初始化，直接返回
        if self._browser_initialized and self.browser:
            return

        try:
            # 确保沙盒环境已准备好
            await self._ensure_sandbox()

            # 启动 Chrome 浏览器（以远程调试模式）
            logger.info("Starting Chrome in remote debugging mode...")
            try:
                # 先检查 Chrome 是否已经在运行
                check_chrome = await self._run_blocking_sandbox_call(
                    "commands.run",
                    lambda: self.sandbox.commands.run("pgrep -f 'chrome.*remote-debugging-port' || echo 'not_running'"),
                )

                if 'not_running' in check_chrome.stdout:
                    # 启动 Chrome 浏览器，监听 9223 端口
                    start_chrome_cmd = """
                    nohup /usr/bin/google-chrome \
                        --no-sandbox \
                        --disable-dev-shm-usage \
                        --remote-debugging-port=9223 \
                        --remote-debugging-address=0.0.0.0 \
                        --headless=new \
                        --disable-gpu \
                        --no-first-run \
                        --no-default-browser-check \
                        --disable-background-networking \
                        --disable-default-apps \
                        --disable-extensions \
                        --disable-sync \
                        --disable-translate \
                        --metrics-recording-only \
                        --mute-audio \
                        --no-zygote \
                        --window-size=1920,1080 \
                        > /tmp/chrome.log 2>&1 &
                    """
                    await self._run_blocking_sandbox_call(
                        "commands.run",
                        lambda: self.sandbox.commands.run(start_chrome_cmd),
                    )
                    logger.info("Chrome start command sent, waiting for browser to start...")

                    # 等待 Chrome 完全启动
                    await asyncio.sleep(5)
                else:
                    logger.info("Chrome is already running")
            except Exception as chrome_start_error:
                logger.warning(f"Failed to start Chrome: {chrome_start_error}")
                # 继续尝试连接，可能 Chrome 已经在运行

            # 获取沙盒的 Chrome 调试端口地址
            host = await self._run_blocking_sandbox_call(
                "get_host",
                lambda: self.sandbox.get_host(9223),
            )
            cdp_url = f"https://{host}"
            logger.info(f"Chrome Debug Protocol URL: {cdp_url}")

            # 创建 Browser 实例使用 Actor API
            self.browser = Browser(cdp_url=cdp_url)
            await self.browser.start()

            self._browser_initialized = True
            logger.info("Browser Actor initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize browser: {e}")
            logger.error(f"Error details: {traceback.format_exc()}")
            raise

    async def _take_screenshot_and_save(self, action_name: str) -> dict:
        """Take screenshot and save to disk, return screenshot info.

        截取当前页面的屏幕截图并保存到本地文件系统。
        生成的截图文件名包含动作名称和时间戳以避免冲突。

        Args:
            action_name: 动作名称，用于生成文件名

        Returns:
            dict: 包含截图URL、文件名、大小等信息的字典，失败返回None
        """
        if not self.current_page:
            logger.warning("No current page available for screenshot")
            return None

        try:
            logger.info(f"Taking screenshot for action: {action_name}")

            # 使用 Page.screenshot() API 获取截图的 base64 数据
            b64img = await self.current_page.screenshot(format="png")

            if not b64img:
                logger.warning("Screenshot returned empty data")
                return None

            # 解码 base64 图像数据
            img_data = base64.b64decode(b64img)

            # 创建截图目录（扁平结构，匹配API端点）
            screenshots_dir = os.path.join(".", "screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)

            # 生成唯一的文件名（包含时间戳避免冲突）
            timestamp = int(time.time())
            safe_action = action_name.replace(" ", "_").replace("/", "_")
            screenshot_filename = f"browser_{safe_action}_{timestamp}.png"
            screenshot_path = os.path.join(screenshots_dir, screenshot_filename)

            # 将图像数据写入文件
            with open(screenshot_path, "wb") as f:
                f.write(img_data)

            # 生成可访问的HTTP URL（匹配API端点路径）
            screenshot_url = f"http://localhost:8000/api/screenshots/{screenshot_filename}"

            # 构建截图信息字典
            screenshot_info = {
                "url": screenshot_url,
                "filename": screenshot_filename,
                "size": len(img_data),
                "content_type": "image/png",
                "description": f"Screenshot: {action_name}"
            }

            logger.info(f"Screenshot saved successfully: {screenshot_path} (size: {len(img_data)} bytes)")
            return screenshot_info

        except Exception as e:
            logger.error(f"Failed to take screenshot: {e}")
            return None

    async def _get_page_info(self) -> tuple[str, str]:
        """Get current page information (URL and title).

        获取当前页面的URL和标题信息。

        Returns:
            tuple[str, str]: (页面URL, 页面标题)，失败时返回空字符串元组
        """
        if not self.current_page:
            return "", ""

        try:
            # 获取页面URL和标题
            url = await self.current_page.get_url()
            title = await self.current_page.get_title()
            return url, title
        except Exception as e:
            logger.warning(f"Failed to get page info: {e}")
            return "", ""

    async def _format_browser_result(self, result_data: dict) -> ToolResult:
        """Format browser result and add to thread manager.

        格式化浏览器操作结果并添加到线程管理器。
        此函数会将结果保存到数据库，并返回格式化的ToolResult对象。

        Args:
            result_data: 包含浏览器操作结果的字典

        Returns:
            ToolResult: 格式化后的工具结果对象
        """
        try:
            # 如果有线程管理器，将消息添加到会话中
            if self.thread_manager:
                thread_id = None
                if hasattr(self.thread_manager, 'thread_id') and self.thread_manager.thread_id:
                    thread_id = self.thread_manager.thread_id

                if thread_id:
                    await self.thread_manager.add_message(
                        thread_id=thread_id,
                        type="browser_state",
                        content=result_data,
                        is_llm_message=False
                    )

            # 构建成功响应对象（确保包含前端需要的所有字段）
            success_response = {
                "success": result_data.get("success", False),
                "message": result_data.get("message", "Browser action completed")
            }

            # 添加前端必需的截图字段（按前端优先级设置）
            if "base64_data" in result_data and result_data["base64_data"]:
                screenshot_url = result_data["base64_data"]

                # Priority 1: image_url (complete HTTP URL)
                success_response["image_url"] = screenshot_url

                # Priority 3: base64_data (for compatibility)
                success_response["base64_data"] = screenshot_url

                # Optional: base_url for relative path concatenation
                success_response["base_url"] = "http://localhost:8000"

            # 如果有screenshot对象，也添加相关信息
            if "screenshot" in result_data and result_data["screenshot"]:
                success_response["screenshot"] = result_data["screenshot"]

            # 添加其他可选字段
            for field in ["url", "title", "elements_found", "scrollable_content", "ocr_text", "message_id"]:
                if field in result_data:
                    success_response[field] = result_data[field]

                    if success_response.get("success"):
                        return ToolResult(success=True, output=json.dumps(success_response, ensure_ascii=False))
                    else:
                        return ToolResult(success=False, output=json.dumps(success_response, ensure_ascii=False))
            else:
                return ToolResult(success=False, output=json.dumps(success_response, ensure_ascii=False))

        except Exception as e:
            logger.error(f"Error formatting browser result: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Error formatting result: {str(e)}"
            }, ensure_ascii=False))

    async def browser_navigate_to(self, url: str) -> ToolResult:
        """Navigate to a specific URL using Actor API.

        使用浏览器Actor API导航到指定的URL。
        此函数会创建新页面（如果需要），加载指定URL，并自动截图保存结果。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_navigate_to",
                "description": "Navigate to a specific url",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The url to navigate to"
                        }
                    },
                    "required": ["url"]
                }
            }
        }

        Example usage:
        >>> await browser_navigate_to(url="https://www.google.com")

        Args:
            url (str): The URL to navigate to

        Returns:
            ToolResult: Result of the execution with screenshot
        """
        try:
            logger.info(f"Navigating to URL: {url}")

            # 确保浏览器已初始化
            await self._ensure_browser()

            # 使用 Browser.new_page() 创建新页面并导航到目标URL
            self.current_page = await self.browser.new_page(url)

            # 页面加载后立即截图
            screenshot_info = await self._take_screenshot_and_save(f"navigate_to_{url}")

            # 获取页面的URL和标题信息
            page_url, page_title = await self._get_page_info()

            # 构造返回结果数据
            result_data = {
                "success": True,
                "message": f"Successfully navigated to: {url}",
                "content": f"Opened page: {page_title}",
                "role": "assistant",
                "url": page_url,
                "title": page_title
            }

            # 添加截图信息到结果中
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info

            return await self._format_browser_result(result_data)

        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Navigation failed: {str(e)}",
                "base64_data": None,
                "screenshot": None,
                "url": url
            }, ensure_ascii=False))

    async def browser_input_text(self, text: str, index: Optional[int] = None, description: Optional[str] = None) -> ToolResult:
        """Input text into an element using Actor API.

        在页面的输入框中输入文本内容。
        可以通过元素索引或描述来定位输入框，如果都不提供则使用第一个找到的输入元素。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_input_text",
                "description": "Input text into an element",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "The index of the element to input text into"
                        },
                        "text": {
                            "type": "string",
                            "description": "The text to input"
                        },
                        "description": {
                            "type": "string",
                            "description": "Description of the element (alternative to index)"
                        }
                    },
                    "required": ["text"]
                }
            }
        }

        Example usage:
        >>> await browser_input_text(text="hello world", index=0)
        >>> await browser_input_text(text="search query", description="search box")

        Args:
            text (str): The text to input
            index (int, optional): The index of the element to input text into
            description (str, optional): Description of the element

        Returns:
            ToolResult: Result of the execution
        """
        try:
            if not self.current_page:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "No page available, please navigate to a URL first"
                }, ensure_ascii=False))

            logger.info(f"Inputting text: {text}")

            if index is not None:
                # 按索引查找输入元素（input、textarea、contenteditable）
                elements = await self.current_page.get_elements_by_css_selector("input, textarea, [contenteditable]")
                if index >= len(elements):
                    raise Exception(f"Index {index} out of range, only found {len(elements)} input elements")

                element = elements[index]
                await element.fill(text)
                action_name = f"input_text_index_{index}"

            elif description:
                # 按描述查找元素（简化处理，实际可以使用LLM进行语义匹配）
                elements = await self.current_page.get_elements_by_css_selector("input, textarea, [contenteditable]")
                if not elements:
                    raise Exception("No input elements found")

                # 简化实现：使用第一个找到的元素
                element = elements[0]
                await element.fill(text)
                action_name = f"input_text_desc"

            else:
                # 自动查找第一个可用的输入元素
                elements = await self.current_page.get_elements_by_css_selector("input, textarea, [contenteditable]")
                if not elements:
                    raise Exception("No input elements found")

                element = elements[0]
                await element.fill(text)
                action_name = f"input_text_auto"

            # 输入后立即截图
            screenshot_info = await self._take_screenshot_and_save(action_name)

            # 获取页面信息
            page_url, page_title = await self._get_page_info()

            # 构造返回结果
            result_data = {
                "success": True,
                "message": f"Successfully input text: {text}",
                "content": f"Text input on page: {text}",
                "role": "assistant",
                "url": page_url,
                "title": page_title
            }

            # 添加截图信息
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info

            return await self._format_browser_result(result_data)

        except Exception as e:
            logger.error(f"Text input failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Text input failed: {str(e)}",
                "base64_data": None,
                "screenshot": None
            }, ensure_ascii=False))

    async def browser_click_element(self, index: Optional[int] = None, description: Optional[str] = None) -> ToolResult:
        """Click on an element using Actor API with enhanced navigation support.

        点击页面上的元素，支持通过索引或描述来定位元素。
        该函数会自动检测点击后是否发生了页面导航，并返回相应的状态。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_click_element",
                "description": "Click on an element by index or description. For search results, use descriptive text like 'first search result' or 'link containing Browser-use'",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {
                            "type": "integer",
                            "description": "The index of the element to click (0-based)"
                        },
                        "description": {
                            "type": "string",
                            "description": "Descriptive text of the element to click, e.g., 'first search result', 'link containing Browser-use', 'search button'"
                        }
                    }
                }
            }
        }

        Example usage:
        >>> await browser_click_element(index=0)
        >>> await browser_click_element(description="first search result")

        Args:
            index (int, optional): The index of the element to click (0-based)
            description (str, optional): Descriptive text of the element to click

        Returns:
            ToolResult: Result of the execution with navigation status
        """
        try:
            if not self.current_page:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "No page available, please navigate to a URL first"
                }, ensure_ascii=False))

            # 记录操作前的URL以便检测是否发生页面导航
            old_url = await self.current_page.get_url()

            if index is not None:
                logger.info(f"Clicking element by index: {index}")
                # 尝试多种点击方式以提高成功率
                try:
                    # Method 1: Use act method with position
                    result = await self.current_page.act(f"click on the element at position {index}")
                except Exception as e1:
                    logger.warning(f"Act method failed, trying alternative: {e1}")
                    try:
                        # Method 2: Use more specific description
                        result = await self.current_page.act(f"click on the {index}th clickable element")
                    except Exception as e2:
                        logger.warning(f"Alternative method failed: {e2}")
                        # Method 3: Use JavaScript to click
                        await self.current_page.evaluate(f"""
                            () => {{
                                const clickableElements = document.querySelectorAll('a, button, input[type="submit"], input[type="button"], [onclick], [role="button"]');
                                if (clickableElements[{index}]) {{
                                    clickableElements[{index}].click();
                                    return true;
                                }}
                                return false;
                            }}
                        """)

            elif description:
                logger.info(f"Clicking element by description: {description}")
                # 尝试多种描述方式以提高元素定位成功率
                try:
                    # Method 1: Direct description
                    result = await self.current_page.act(f"click on {description}")
                except Exception as e1:
                    logger.warning(f"Direct description failed: {e1}")
                    try:
                        # Method 2: More specific description based on keywords
                        if "first" in description.lower():
                            result = await self.current_page.act("click on the first search result link")
                        elif "search result" in description.lower():
                            result = await self.current_page.act("click on a search result")
                        else:
                            result = await self.current_page.act(f"click on the link or button with text '{description}'")
                    except Exception as e2:
                        logger.warning(f"Specific description failed: {e2}")
                        # Method 3: Use JavaScript to find and click
                        await self.current_page.evaluate(f"""
                            () => {{
                                const links = Array.from(document.querySelectorAll('a'));
                                const targetLink = links.find(link =>
                                    link.textContent.toLowerCase().includes('{description.lower()}') ||
                                    link.href.toLowerCase().includes('{description.lower()}')
                                );
                                if (targetLink) {{
                                    targetLink.click();
                                    return true;
                                }}

                                const firstResult = document.querySelector('h3 a, .result a, .c-container a');
                                if (firstResult) {{
                                    firstResult.click();
                                    return true;
                                }}
                                return false;
                            }}
                        """)
            else:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "Must provide either index or description parameter"
                }, ensure_ascii=False))

            # 等待点击操作完成和可能的页面导航
            await asyncio.sleep(2.0)

            # 检测是否发生了页面导航
            new_url = await self.current_page.get_url()
            navigation_occurred = old_url != new_url

            # 点击后立即截图
            if index is not None:
                screenshot_info = await self._take_screenshot_and_save(f"click_index_{index}")
            else:
                screenshot_info = await self._take_screenshot_and_save(f"click_desc")

            # 获取当前页面信息
            page_url, page_title = await self._get_page_info()

            # 构造返回结果，包含导航信息
            if navigation_occurred:
                message = f"Successfully clicked element and navigated to new page: {new_url}"
            else:
                message = "Successfully clicked element (no navigation occurred)"

            result_data = {
                "success": True,
                "message": message,
                "content": "Element clicked",
                "role": "assistant",
                "url": page_url,
                "title": page_title,
                "navigation_occurred": navigation_occurred,
                "old_url": old_url,
                "new_url": new_url
            }

            # 添加截图信息
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info

            return await self._format_browser_result(result_data)

        except Exception as e:
            logger.error(f"Click failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Click failed: {str(e)}",
                "base64_data": None,
                "screenshot": None
            }, ensure_ascii=False))

    async def browser_send_keys(self, keys: str) -> ToolResult:
        """Send keyboard keys using Actor API.

        发送键盘按键，如回车键、ESC键或快捷键组合。
        常用于提交表单、关闭对话框等操作。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_send_keys",
                "description": "Send keyboard keys such as Enter, Escape, or keyboard shortcuts",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keys": {
                            "type": "string",
                            "description": "The keys to send (e.g., 'Enter', 'Escape', 'Control+a')"
                        }
                    },
                    "required": ["keys"]
                }
            }
        }

        Example usage:
        >>> await browser_send_keys(keys="Enter")
        >>> await browser_send_keys(keys="Control+a")

        Args:
            keys (str): The keys to send (e.g., 'Enter', 'Escape', 'Control+a')

        Returns:
            ToolResult: Result of the execution
        """
        try:
            if not self.current_page:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "No page available, please navigate to a URL first"
                }, ensure_ascii=False))

            logger.info(f"Sending keyboard keys: {keys}")

            # 使用 Page.press() API 发送按键到页面
            await self.current_page.press(keys)

            # 等待页面响应按键操作
            await asyncio.sleep(1)

            # 按键后立即截图
            screenshot_info = await self._take_screenshot_and_save(f"send_keys_{keys}")

            # 获取页面信息
            page_url, page_title = await self._get_page_info()

            # 构造返回结果
            result_data = {
                "success": True,
                "message": f"Successfully sent keys: {keys}",
                "content": f"Sent keys: {keys}",
                "role": "assistant",
                "url": page_url,
                "title": page_title
            }

            # 添加截图信息
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info

            return await self._format_browser_result(result_data)

        except Exception as e:
            logger.error(f"Send keys failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Send keys failed: {str(e)}",
                "base64_data": None,
                "screenshot": None
            }, ensure_ascii=False))

    async def browser_scroll_down(self, amount: Optional[int] = None) -> ToolResult:
        """Scroll down the page using Actor API.

        向下滚动页面，用于查看页面下方的内容。
        默认滚动800像素，可以自定义滚动距离（最小200像素）。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_scroll_down",
                "description": "Scroll down the page",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "amount": {
                            "type": "integer",
                            "description": "Scroll amount in pixels (minimum 200px, recommended 500-1000px for noticeable effect). If not specified, defaults to 800px for effective scrolling."
                        }
                    }
                }
            }
        }

        Example usage:
        >>> await browser_scroll_down()  # Default 800px
        >>> await browser_scroll_down(amount=1000)

        Args:
            amount (int, optional): Scroll amount in pixels. Defaults to 800px.

        Returns:
            ToolResult: Result of the execution
        """
        try:
            if not self.current_page:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "No page available, please navigate to a URL first"
                }, ensure_ascii=False))

            # 设置滚动距离（默认800px，最小200px以确保滚动效果明显）
            if amount is not None:
                    scroll_amount = max(amount, 200)
            else:
                    scroll_amount = 800

            logger.info(f"Scrolling down: {scroll_amount}px")

            # 使用 JavaScript 执行页面滚动
            await self.current_page.evaluate(f"() => window.scrollBy(0, {scroll_amount})")

            # 等待滚动动画完成和页面内容加载
            await asyncio.sleep(1.2)

            # 滚动后立即截图
            screenshot_info = await self._take_screenshot_and_save(f"scroll_down_{scroll_amount}")

            # 获取页面信息
            page_url, page_title = await self._get_page_info()

            # 构造返回结果
            result_data = {
                "success": True,
                "message": f"Successfully scrolled down {scroll_amount}px",
                "content": "Page scrolled down",
                "role": "assistant",
                "url": page_url,
                "title": page_title
            }

            # 添加截图信息
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info

            return await self._format_browser_result(result_data)

        except Exception as e:
            logger.error(f"Scroll failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Scroll failed: {str(e)}",
                "base64_data": None,
                "screenshot": None
            }, ensure_ascii=False))

    async def browser_scroll_up(self, amount: Optional[int] = None) -> ToolResult:
        """Scroll up the page using Actor API.

        向上滚动页面，用于返回查看页面上方的内容。
        默认滚动800像素，可以自定义滚动距离（最小200像素）。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_scroll_up",
                "description": "Scroll up the page",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "amount": {
                            "type": "integer",
                            "description": "Scroll amount in pixels (minimum 200px, recommended 500-1000px for noticeable effect). If not specified, defaults to 800px for effective scrolling."
                        }
                    }
                }
            }
        }

        Example usage:
        >>> await browser_scroll_up()  # Default 800px
        >>> await browser_scroll_up(amount=500)

        Args:
            amount (int, optional): Scroll amount in pixels. Defaults to 800px.

        Returns:
            ToolResult: Result of the execution
        """
        try:
            if not self.current_page:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "No page available, please navigate to a URL first"
                }, ensure_ascii=False))

            # 设置滚动距离（默认800px，最小200px以确保滚动效果明显）
            if amount is not None:
                    scroll_amount = max(amount, 200)
            else:
                    scroll_amount = 800

            logger.info(f"Scrolling up: {scroll_amount}px")

            # 使用 JavaScript 执行页面向上滚动（负数表示向上）
            await self.current_page.evaluate(f"() => window.scrollBy(0, -{scroll_amount})")

            # 等待滚动动画完成和页面内容加载
            await asyncio.sleep(1.2)

            # 滚动后立即截图
            screenshot_info = await self._take_screenshot_and_save(f"scroll_up_{scroll_amount}")

            # 获取页面信息
            page_url, page_title = await self._get_page_info()

            # 构造返回结果
            result_data = {
                "success": True,
                "message": f"Successfully scrolled up {scroll_amount}px",
                "content": "Page scrolled up",
                "role": "assistant",
                "url": page_url,
                "title": page_title
            }

            # 添加截图信息
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info

            return await self._format_browser_result(result_data)

        except Exception as e:
            logger.error(f"Scroll failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Scroll failed: {str(e)}",
                "base64_data": None,
                "screenshot": None
            }, ensure_ascii=False))

    async def browser_go_back(self) -> ToolResult:
        """Navigate back in browser history using JavaScript.

        返回浏览器历史记录的上一页。
        等同于点击浏览器的后退按钮。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_go_back",
                "description": "Navigate back in browser history",
                "parameters": {
                    "type": "object",
                    "properties": {}
                }
            }
        }

        Example usage:
        >>> await browser_go_back()

        Returns:
            ToolResult: Result of the execution
        """
        try:
            if not self.current_page:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "No page available, please navigate to a URL first"
                }, ensure_ascii=False))

            logger.info("Navigating back in browser history")

            # 使用 JavaScript 执行浏览器后退操作
            await self.current_page.evaluate("() => window.history.back()")

            # 等待页面后退加载完成
            await asyncio.sleep(2)

            # 后退后立即截图
            screenshot_info = await self._take_screenshot_and_save("go_back")

            # 获取页面信息
            page_url, page_title = await self._get_page_info()

            # 构造返回结果
            result_data = {
                "success": True,
                "message": "Successfully navigated back",
                "content": "Navigated back to previous page in history",
                "role": "assistant",
                "url": page_url,
                "title": page_title
            }

            # 添加截图信息
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info

            return await self._format_browser_result(result_data)

        except Exception as e:
            logger.error(f"Go back failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Go back failed: {str(e)}",
                "base64_data": None,
                "screenshot": None
            }, ensure_ascii=False))

    async def browser_wait(self, seconds: int = 3) -> ToolResult:
        """Wait for the specified number of seconds.

        等待指定的秒数，常用于等待页面加载或动态内容渲染。
        等待期间可以让JavaScript执行完成或等待AJAX请求。

        Few-shot Example:
        {
            "type": "function",
            "function": {
                "name": "browser_wait",
                "description": "Wait for the specified number of seconds",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "seconds": {
                            "type": "integer",
                            "description": "Number of seconds to wait (default: 3)"
                        }
                    }
                }
            }
        }

        Example usage:
        >>> await browser_wait(seconds=5)
        >>> await browser_wait()  # Default 3 seconds

        Args:
            seconds (int): Number of seconds to wait. Defaults to 3.

        Returns:
            ToolResult: Result of the execution
        """
        try:
            await asyncio.sleep(seconds)
            
            # 如果有当前页面，截图
            screenshot_info = None
            if self.current_page:
                screenshot_info = await self._take_screenshot_and_save(f"wait_{seconds}s")
            
            # 获取页面信息
            page_url, page_title = await self._get_page_info()
            
            # 构造返回结果
            result_data = {
                "success": True,
                "message": f"Wait completed: {seconds} seconds",
                "content": f"Waited for {seconds} seconds",
                "role": "assistant",
                "url": page_url,
                "title": page_title
            }
            
            # 添加截图信息
            if screenshot_info:
                result_data["base64_data"] = screenshot_info["url"]
                result_data["screenshot"] = screenshot_info
            
            return await self._format_browser_result(result_data)
            
        except Exception as e:
            logger.error(f"Wait failed: {e}")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Wait failed: {str(e)}",
                "base64_data": None,
                "screenshot": None
            }, ensure_ascii=False))

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        if self.browser:
            try:
                await self.browser.stop()
            except Exception as e:
                logger.warning(f"Error stopping browser: {e}")
        if self._sandbox:
            try:
                await self._run_blocking_sandbox_call("kill", lambda: self.sandbox.kill())
            except Exception as e:
                logger.warning(f"Error killing sandbox: {e}")
