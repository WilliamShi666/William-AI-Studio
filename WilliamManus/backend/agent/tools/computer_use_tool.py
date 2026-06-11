"""
sandbox.move_mouse(x, y)	移动鼠标	move_to()
sandbox.left_click(x, y)	左键点击	click()
sandbox.right_click(x, y)	右键点击	click()
sandbox.double_click(x, y)	双击	click()
sandbox.scroll(direction, amount)	滚动	scroll()
sandbox.write(text)	输入文本	typing()
sandbox.press(keys)	按键	press()
sandbox.mouse_press(button)	按下鼠标	mouse_down()
sandbox.mouse_release(button)	释放鼠标	mouse_up()
sandbox.drag(from, to)	拖拽	drag_to()
sandbox.wait(milliseconds)	等待	wait()
sandbox.screenshot(format='bytes')	截图	screenshot()
sandbox.get_screen_size()	获取屏幕尺寸	screenshot()
sandbox.stream.start()	启动 VNC 流	沙箱创建时
sandbox.stream.get_url()	获取 VNC URL	沙箱创建时
"""
from __future__ import annotations

import asyncio
import json
import logging
import base64
from typing import Dict, List, Optional, TYPE_CHECKING
from datetime import datetime

from agentpress.tool import Tool, ToolResult
from sandbox.tool_base import SandboxToolsBase
from utils.logger import logger

if TYPE_CHECKING:
    from agentpress.adk_thread_manager import ADKThreadManager
# 移除Supabase Storage依赖，直接返回base64数据

# Supported keyboard keys for the press method
KEYBOARD_KEYS = [
    'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i', 'j', 'k', 'l', 'm',
    'n', 'o', 'p', 'q', 'r', 's', 't', 'u', 'v', 'w', 'x', 'y', 'z',
    '0', '1', '2', '3', '4', '5', '6', '7', '8', '9',
    'space', 'enter', 'tab', 'escape', 'backspace', 'delete',
    'shift', 'ctrl', 'alt', 'cmd', 'meta', 'super', 'win', 'windows',
    'left', 'right', 'up', 'down',
    'home', 'end', 'pageup', 'pagedown', 'insert',
    'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12',
    'capslock', 'numlock', 'scrolllock', 'printscreen'
]


class ComputerUseTool(SandboxToolsBase):
    """Computer automation tool for controlling the sandbox desktop using E2B Desktop SDK directly."""
    
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
            sandbox_type='desktop',
            shadow_clone_run_id=shadow_clone_run_id,
            strict_sandbox=strict_sandbox,
        )
        self.mouse_x = 0
        self.mouse_y = 0

    
    def _handle_tool_error(self, error: Exception, action: str) -> str:
        """
        处理工具错误并提供用户友好的错误消息
        Handle tool errors and provide user-friendly messages.
        """
        error_str = str(error)
        normalized_error = error_str.lower()
        if self._strict_sandbox_mode and (
            "could not be reattached for run" in normalized_error
            or "strict single-sandbox mode forbids creating a replacement sandbox"
            in normalized_error
            or "shadow clone sandbox" in normalized_error
        ):
            return error_str
        if "沙箱对象为空" in error_str or "沙箱环境未就绪" in error_str:
            return f"Sandbox environment not ready, please try again later"
        elif "Sandbox" in error_str and ("not found" in error_str or "not accessible" in error_str):
            return f"Sandbox environment not accessible, please recreate the project"
        elif "timeout" in error_str.lower():
            return f"Operation timeout, please check network connection"
        elif "can only concatenate str" in error_str:
            return f"Sandbox environment configuration error, please try again later"
        else:
            return f"Error executing {action}: {error_str}"

    async def _desktop_call(self, operation: str, call):
        return await self._run_blocking_sandbox_call(operation, call)

    async def move_to(self, x: float, y: float) -> ToolResult:
        """Move cursor to specified coordinates.

        Usage Examples:
            {
                "name": "move_to",
                "parameters": {
                    "x": 100,
                    "y": 200
                }
            }

        Args:
            x (float): X coordinate (pixels from left edge)
            y (float): Y coordinate (pixels from top edge)

        Returns:
            ToolResult: Success with position confirmation, or failure with error message.
        """
        try:
            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            x_int = int(round(x))
            y_int = int(round(y))

            # 直接调用E2B SDK的鼠标移动方法
            await self._desktop_call("move_mouse", lambda: self.sandbox.move_mouse(x_int, y_int))

            # 更新内部鼠标位置记录
            self.mouse_x = x_int
            self.mouse_y = y_int
            return ToolResult(success=True, output=f"Mouse moved to coordinates ({x_int}, {y_int})")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "move mouse")
            return ToolResult(success=False, output=f"Failed to move mouse: {error_msg}")

    async def click(self, x: int, y: int, button: str = "left", num_clicks: int = 1) -> ToolResult:
        """Click at specified coordinates with desktop environment awareness.

        Args:
            x (int): X coordinate
            y (int): Y coordinate
            button (str): Mouse button ("left", "right", "middle")
            num_clicks (int): Number of clicks (1 for single, 2 for double)

        Returns:
            ToolResult: Success with click confirmation, or failure with error message.
        """
        try:
            # 坐标验证：不允许负数
            if x < 0 or y < 0:
                return ToolResult(success=False, output="Coordinates cannot be negative")

            if button not in ["left", "right", "middle"]:
                button = "left"

            num_clicks = max(1, int(num_clicks))

            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            # 基于坐标位置提供智能提示（帮助Agent理解桌面布局）
            click_advice = ""
            if y < 150 and x < 150:
                click_advice = " (Warning: Top-left corner - desktop icons area. Check bottom taskbar for applications)"
            elif y > 900:
                click_advice = " (Info: Bottom taskbar area, applications are typically here)"
            elif x < 100:
                click_advice = " (Warning: Left-side desktop icons area, usually folders not applications)"

            # 根据点击次数调用对应的SDK方法
            if num_clicks == 1:
                if button == "left":
                    await self._desktop_call("left_click", lambda: self.sandbox.left_click(x, y))
                elif button == "right":
                    await self._desktop_call("right_click", lambda: self.sandbox.right_click(x, y))
                else:
                    await self._desktop_call("middle_click", lambda: self.sandbox.middle_click(x, y))
            else:
                await self._desktop_call("double_click", lambda: self.sandbox.double_click(x, y))

            return ToolResult(success=True, output=f"Clicked at ({x}, {y}) with {num_clicks} {button} click(s){click_advice}")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "click")
            return ToolResult(success=False, output=f"Failed to click: {error_msg}")

    async def scroll(self, amount: int) -> ToolResult:
        """Scroll up or down by specified amount.

        Usage Examples:
            {
                "name": "scroll",
                "parameters": {
                    "amount": -3
                }
            }

        Args:
            amount (int): Scroll amount (positive=down, negative=up)

        Returns:
            ToolResult: Success with scroll confirmation, or failure with error message.
        """
        try:
            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            amount_int = int(amount)
            # 根据正负值确定滚动方向
            direction_text = "down" if amount_int > 0 else "up"
            sdk_direction = "down" if amount_int > 0 else "up"

            # 调用E2B SDK的滚动方法
            await self._desktop_call("scroll", lambda: self.sandbox.scroll(direction=sdk_direction, amount=abs(amount_int)))

            return ToolResult(success=True, output=f"Scrolled {direction_text} by {abs(amount_int)} units")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "scroll")
            return ToolResult(success=False, output=f"Failed to scroll: {error_msg}")

    async def typing(self, text: str) -> ToolResult:
        """Type text at current cursor position.

        Usage Examples:
            {
                "name": "typing",
                "parameters": {
                    "text": "Hello World"
                }
            }

        Args:
            text (str): Text to type

        Returns:
            ToolResult: Success with text confirmation, or failure with error message.
        """
        try:
            if not text:
                return ToolResult(success=False, output="Input text cannot be empty")

            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            # 检测并处理非ASCII字符（沙箱环境可能不支持中文输入）
            if any(ord(char) > 127 for char in text):
                # 常见中文词汇的英文转换映射
                if text == "浏览器":
                    text = "firefox"
                    logger.warning("Converted Chinese input '浏览器' to 'firefox'")
                elif text == "谷歌":
                    text = "google"
                    logger.warning("Converted Chinese input '谷歌' to 'google'")
                elif text == "搜索":
                    text = "search"
                    logger.warning("Converted Chinese input '搜索' to 'search'")
                else:
                    # 尝试编码处理其他非ASCII字符
                    try:
                        encoded_text = text.encode('utf-8', errors='ignore').decode('ascii', errors='ignore')
                        if encoded_text:
                            text = encoded_text
                        else:
                            return ToolResult(success=False, output=f"Input contains unsupported characters, please use English: {text}")
                    except Exception:
                        return ToolResult(success=False, output=f"Character encoding conversion failed, please use English: {text}")

            # 调用E2B SDK的文本输入方法
            await self._desktop_call("write", lambda: self.sandbox.write(str(text)))

            return ToolResult(success=True, output=f"Typed text: {text}")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "type text")
            # 如果是编码错误，提供更具体的建议
            if "multi-byte sequence" in str(e) or "encoding" in str(e).lower():
                return ToolResult(success=False, output=f"Character encoding error, please use English input. Original error: {error_msg}")
            return ToolResult(success=False, output=f"Failed to type text: {error_msg}")

    async def press(self, keys: str) -> ToolResult:
        """Press keyboard keys or key combinations.

        Usage Examples:
            {
                "name": "press",
                "parameters": {
                    "keys": "enter"
                }
            }
            {
                "name": "press",
                "parameters": {
                    "keys": "ctrl+c"
                }
            }
            {
                "name": "press",
                "parameters": {
                    "keys": "alt+tab"
                }
            }

        Args:
            keys (str): Keys to press (e.g., "enter", "ctrl+c", "alt+tab")

        Returns:
            ToolResult: Success with key confirmation, or failure with error message.
        """
        try:
            if not keys:
                return ToolResult(success=False, output="Key input cannot be empty")

            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            # 解析按键，将字符串分割为按键列表
            key_parts = [k.strip().lower() for k in keys.split('+')]

            # 调用E2B SDK的按键方法 - 支持单键和组合键
            if '+' in keys:
                # 组合键，传递列表（如 ['ctrl', 'c']）
                await self._desktop_call("press_combo", lambda: self.sandbox.press(key_parts))
            else:
                # 单键，传递字符串（如 'enter'）
                await self._desktop_call("press_key", lambda: self.sandbox.press(keys.lower()))

            return ToolResult(success=True, output=f"Pressed key(s): {keys}")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "press key")
            return ToolResult(success=False, output=f"Failed to press key: {error_msg}")

    async def wait(self, seconds: float) -> ToolResult:
        """Wait for specified number of seconds.

        Usage Examples:
            {
                "name": "wait",
                "parameters": {
                    "seconds": 2.5
                }
            }

        Args:
            seconds (float): Number of seconds to wait

        Returns:
            ToolResult: Success with wait confirmation.
        """
        try:
            # 等待时间验证
            if seconds < 0:
                return ToolResult(success=False, output="Wait time cannot be negative")
            if seconds > 30:
                return ToolResult(success=False, output="Wait time cannot exceed 30 seconds")

            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            wait_time = float(seconds)
            # 调用E2B SDK的等待方法（参数单位为毫秒）
            await self._desktop_call("wait", lambda: self.sandbox.wait(int(wait_time * 1000)))

            return ToolResult(success=True, output=f"Waited for {wait_time} seconds")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "wait")
            return ToolResult(success=False, output=f"Failed to wait: {error_msg}")

    async def mouse_down(self, x: float, y: float,
                        button: str = "left") -> ToolResult:
        """Press and hold mouse button at specified coordinates.

        Usage Examples:
            {
                "name": "mouse_down",
                "parameters": {
                    "x": 100,
                    "y": 200
                }
            }

        Args:
            x (float): X coordinate
            y (float): Y coordinate
            button (str): Mouse button to press

        Returns:
            ToolResult: Success with button press confirmation, or failure with error message.
        """
        try:
            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            x_int = int(round(x))
            y_int = int(round(y))

            # 验证按键参数
            if button not in ["left", "right", "middle"]:
                button = "left"

            # 先移动鼠标到目标位置，再按下按钮
            await self._desktop_call("move_mouse", lambda: self.sandbox.move_mouse(x_int, y_int))
            await self._desktop_call("mouse_press", lambda: self.sandbox.mouse_press(button))

            # 更新内部鼠标位置记录
            self.mouse_x = x_int
            self.mouse_y = y_int
            return ToolResult(success=True, output=f"Mouse button '{button}' pressed at ({x_int}, {y_int})")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "mouse down")
            return ToolResult(success=False, output=f"Failed to press mouse button: {error_msg}")

    async def mouse_up(self, button: str = "left") -> ToolResult:
        """Release mouse button.

        Usage Examples:
            {
                "name": "mouse_up",
                "parameters": {
                    "button": "left"
                }
            }

        Args:
            button (str): Mouse button to release

        Returns:
            ToolResult: Success with button release confirmation, or failure with error message.
        """
        try:
            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            # 验证按键参数
            if button not in ["left", "right", "middle"]:
                button = "left"

            # 调用E2B SDK的鼠标释放方法
            await self._desktop_call("mouse_release", lambda: self.sandbox.mouse_release(button))

            return ToolResult(success=True, output=f"Mouse button '{button}' released")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "mouse up")
            return ToolResult(success=False, output=f"Failed to release mouse button: {error_msg}")

    def _clean_old_screenshots(self, max_files: int = 50):
        """
        清理旧的截图文件，保留最新的max_files个文件
        Clean up old screenshot files, keep only the latest max_files screenshots.
        """
        try:
            from pathlib import Path
            screenshots_dir = Path("screenshots")
            if not screenshots_dir.exists():
                return

            # 获取所有截图文件
            screenshot_files = list(screenshots_dir.glob("screenshot_*.png"))

            # 如果文件数量超过限制，删除最旧的文件
            if len(screenshot_files) > max_files:
                # 按修改时间排序（从旧到新）
                screenshot_files.sort(key=lambda x: x.stat().st_mtime)

                # 删除最旧的文件（保留最后max_files个）
                files_to_delete = screenshot_files[:-max_files]
                for file_path in files_to_delete:
                    try:
                        file_path.unlink()
                        logger.debug(f"Cleaned up old screenshot: {file_path.name}")
                    except Exception as e:
                        logger.warning(f"Failed to delete screenshot {file_path.name}: {e}")

        except Exception as e:
            logger.warning(f"Screenshot cleanup failed: {e}")

    async def drag_to(self, x: float, y: float) -> ToolResult:
        """Drag cursor to specified position.

        Usage Examples:
            {
                "name": "drag_to",
                "parameters": {
                    "x": 300,
                    "y": 400
                }
            }

        Args:
            x (float): Target X coordinate
            y (float): Target Y coordinate

        Returns:
            ToolResult: Success with drag confirmation, or failure with error message.
        """
        try:
            # 确保沙箱环境已准备好
            await self._ensure_sandbox()

            x_int = int(round(x))
            y_int = int(round(y))
            # 记录起始位置（当前鼠标位置）
            start_x = self.mouse_x
            start_y = self.mouse_y

            # 调用E2B SDK的拖拽方法（从当前位置拖拽到目标位置）
            await self._desktop_call("drag", lambda: self.sandbox.drag((start_x, start_y), (x_int, y_int)))

            # 更新内部鼠标位置记录
            self.mouse_x = x_int
            self.mouse_y = y_int
            return ToolResult(success=True,
                output=f"Dragged from ({start_x}, {start_y}) to ({x_int}, {y_int})")

        except Exception as e:
            error_msg = self._handle_tool_error(e, "drag")
            return ToolResult(success=False, output=f"Failed to drag: {error_msg}")

    async def screenshot(self) -> ToolResult:
        """
        截取当前桌面屏幕截图
        Take a screenshot of current desktop.
        """
        try:
            # 确保沙箱环境已准备好
            await self._ensure_sandbox()
            # 调用E2B SDK截图方法，获取图像字节数据
            screenshot_bytes = await self._desktop_call("screenshot", lambda: self.sandbox.screenshot(format='bytes'))

            if screenshot_bytes:
                timestamp = datetime.now().isoformat()

                # 创建截图保存目录
                import os
                screenshots_dir = "screenshots"
                os.makedirs(screenshots_dir, exist_ok=True)

                # 生成唯一的截图文件名
                from uuid import uuid4
                screenshot_filename = f"screenshot_{uuid4().hex}.png"
                screenshot_path = os.path.join(screenshots_dir, screenshot_filename)

                # 保存截图到本地文件
                with open(screenshot_path, 'wb') as f:
                    f.write(screenshot_bytes)

                # 清理旧截图文件（保持文件数量在限制内）
                self._clean_old_screenshots(max_files=50)

                # 获取屏幕尺寸
                try:
                    width, height = await self._desktop_call("get_screen_size", lambda: self.sandbox.get_screen_size())
                except:
                    width, height = 1024, 768  # 默认尺寸

                # 构建截图URL（用于前端访问）
                screenshot_url = f"http://localhost:8000/api/screenshots/{screenshot_filename}"

                # 构建返回结果
                result = {
                    "success": True,
                    "message": f"Screenshot captured successfully (size: {width}x{height}, {len(screenshot_bytes)} bytes)",
                    "timestamp": timestamp,
                    "screenshot": {
                        "width": width,
                        "height": height,
                        "size": len(screenshot_bytes),
                        "content_type": "image/png",
                        "url": screenshot_url,
                        "filename": screenshot_filename,
                        "base64": None,  # 不包含base64数据以避免占用过多上下文窗口
                        "description": "Screenshot has been generated and saved locally"
                    },
                    "base64_data": screenshot_url,  # 用于agent/run.py兼容性
                    "desktop_stream": getattr(self, 'sandbox_metadata', {}).get('vnc_preview', {}),  # 安全获取VNC预览信息
                    "action_required": "Carefully analyze this screenshot content, confirm the current desktop state, and plan the next action based on what you actually see. Don't assume anything!"
                }
                return ToolResult(success=True, output=json.dumps(result, ensure_ascii=False))
            else:
                return ToolResult(success=False, output=json.dumps({
                    "success": False,
                    "message": "Screenshot failed: No image data retrieved"
                }, ensure_ascii=False))
        except Exception as e:
            error_msg = self._handle_tool_error(e, "screenshot")
            return ToolResult(success=False, output=json.dumps({
                "success": False,
                "message": f"Screenshot failed: {error_msg}"
            }, ensure_ascii=False))

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        pass 
