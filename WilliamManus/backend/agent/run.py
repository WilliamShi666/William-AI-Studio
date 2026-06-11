from __future__ import annotations

import os
import json
import asyncio
import datetime
from typing import Optional, Dict, List, Any, AsyncGenerator, TYPE_CHECKING
from dataclasses import dataclass
import traceback
from uuid import uuid4

from dotenv import load_dotenv  # type: ignore
from utils.config import config
from agent.prompt import get_system_prompt
from agent.gemini_prompt import get_gemini_system_prompt
from utils.logger import logger
from services.langfuse import langfuse
from .media_payload import parse_user_event_content, payload_to_runner_input
from agentscope_integration.shadow_clone.constants import ShadowCloneMode

if TYPE_CHECKING:
    from agentpress.thread_manager import ThreadManager

if TYPE_CHECKING:
    from agentpress.thread_manager import ThreadManager

# AgentScope Integration
from agentscope_integration import AgentScopeRunner
from agentscope_integration.claude_sdk_runner import ClaudeSDKRunner

# Langfuse stateful client types: removed in v3 (`langfuse.client` module gone). Use Any.
try:
    from langfuse._client.span import LangfuseSpan as StatefulTraceClient  # type: ignore
except Exception:
    from typing import Any

    StatefulTraceClient = Any  # type: ignore


load_dotenv(override=False)


def _normalize_agent_backend(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _is_claude_sdk_backend(value: Any) -> bool:
    return _normalize_agent_backend(value) in (
        "claude",
        "claude_sdk",
        "claude_agent_sdk",
        "claudeagentsdk",
    )


def _agent_config_requests_claude_sdk_backend(agent_config: Optional[dict]) -> bool:
    if not isinstance(agent_config, dict):
        return False

    for key in ("agent_backend", "agentBackend", "backend"):
        if _is_claude_sdk_backend(agent_config.get(key)):
            return True
    return False


def _build_multi_agent_mode_instruction(mode: Any) -> str:
    normalized_mode = (
        str(getattr(mode, "value", mode) or "").strip().lower().replace("-", "_")
    )
    if normalized_mode == ShadowCloneMode.ON.value:
        return (
            "Multi-agent mode selected by the user: On. The user wants to enable "
            "multiple shadow clones/subagents to execute this run when useful."
        )
    if normalized_mode == ShadowCloneMode.AUTO.value:
        return (
            "Multi-agent mode selected by the user: Auto. The user wants the main "
            "agent to decide whether multiple shadow clones/subagents are needed "
            "to execute this run."
        )
    return ""


def _augment_user_message_with_multi_agent_mode(user_message: str, mode: Any) -> str:
    instruction = _build_multi_agent_mode_instruction(mode)
    if not instruction:
        return user_message

    base_message = str(user_message or "")
    if not base_message:
        return instruction
    return f"{base_message}\n\n[Runtime multi-agent intent]\n{instruction}"


@dataclass
class AgentConfig:
    thread_id: str
    project_id: str
    stream: bool
    native_max_auto_continues: int = 0
    max_iterations: int = 100
    model_name: str = config.MODEL_TO_USE or "gemini/gemini-3.1-pro-preview"
    enable_thinking: Optional[bool] = False
    reasoning_effort: Optional[str] = "low"
    enable_context_manager: bool = True
    agent_config: Optional[dict] = None
    trace: Optional[StatefulTraceClient] = None  # type: ignore
    is_agent_builder: Optional[bool] = False
    target_agent_id: Optional[str] = None
    resume_strategy: str = "auto"
    resume_window_minutes: int = 1440
    user_message_override: Optional[str] = None
    agent_run_id: Optional[str] = None
    shadow_clone_mode: ShadowCloneMode = ShadowCloneMode.OFF
    shadow_clone_main_model: Optional[str] = None
    shadow_clone_subagent_model: Optional[str] = None


class ToolManager:
    def __init__(self, thread_manager: ThreadManager, project_id: str, thread_id: str):
        self.thread_manager = thread_manager
        self.project_id = project_id
        self.thread_id = thread_id

    def register_all_tools(self):
        # # 测试现有工具注册流程
        from agent.tools.simple_test_tool import SimpleTestTool

        self.thread_manager.add_tool(SimpleTestTool)

        from agent.tools.task_list_tool import TaskListTool

        self.thread_manager.add_tool(
            TaskListTool,
            project_id=self.project_id,
            thread_manager=self.thread_manager,
            thread_id=self.thread_id,
        )

        from agent.tools.sandbox_web_search_tool import SandboxWebSearchTool

        self.thread_manager.add_tool(
            SandboxWebSearchTool,
            project_id=self.project_id,
            thread_manager=self.thread_manager,
        )

        from agent.tools.sandbox_code_tool import SandboxCodeTool

        self.thread_manager.add_tool(
            SandboxCodeTool,
            function_names=[
                "execute_command",
                "read_file",
                "write_file",
                "list_dir",
                "make_dir",
                "upload_file",
                "download_file",
                "expose_port",
            ],
            project_id=self.project_id,
            thread_manager=self.thread_manager,
        )

        from agent.tools.sandbox_skill_tool import SandboxSkillTool

        self.thread_manager.add_tool(
            SandboxSkillTool,
            function_names=[
                "get_available_skills",
                "load_skill",
                "load_reference",
                "list_skill_scripts",
                "run_skill_script",
            ],
            project_id=self.project_id,
            thread_manager=self.thread_manager,
        )

        from agent.tools.computer_use_tool import ComputerUseTool

        self.thread_manager.add_tool(
            ComputerUseTool,
            project_id=self.project_id,
            thread_manager=self.thread_manager,
        )

        from agent.tools.sb_browser_tool import SandboxBrowserTool

        self.thread_manager.add_tool(
            SandboxBrowserTool,
            project_id=self.project_id,
            thread_manager=self.thread_manager,
        )

    def register_agent_builder_tools(self, agent_id: str):
        # TODO
        pass

    def register_custom_tools(self, enabled_tools: Dict[str, Any]):
        # TODO
        pass


# class MCPManager:
#     def __init__(self, thread_manager: ThreadManager, account_id: str):
#         self.thread_manager = thread_manager
#         self.account_id = account_id

#     async def register_mcp_tools(self, agent_config: dict) -> Optional[MCPToolWrapper]:
#         all_mcps = []

#         if agent_config.get('configured_mcps'):
#             all_mcps.extend(agent_config['configured_mcps'])

#         if agent_config.get('custom_mcps'):
#             for custom_mcp in agent_config['custom_mcps']:
#                 custom_type = custom_mcp.get('customType', custom_mcp.get('type', 'sse'))

#                 if custom_type == 'pipedream':
#                     if 'config' not in custom_mcp:
#                         custom_mcp['config'] = {}

#                     if not custom_mcp['config'].get('external_user_id'):
#                         profile_id = custom_mcp['config'].get('profile_id')
#                         if profile_id:
#                             try:
#                                 from pipedream import profile_service
#                                 from uuid import UUID

#                                 profile = await profile_service.get_profile(UUID(self.account_id), UUID(profile_id))
#                                 if profile:
#                                     custom_mcp['config']['external_user_id'] = profile.external_user_id
#                             except Exception as e:
#                                 logger.error(f"Error retrieving external_user_id from profile {profile_id}: {e}")

#                     if 'headers' in custom_mcp['config'] and 'x-pd-app-slug' in custom_mcp['config']['headers']:
#                         custom_mcp['config']['app_slug'] = custom_mcp['config']['headers']['x-pd-app-slug']

#                 elif custom_type == 'composio':
#                     qualified_name = custom_mcp.get('qualifiedName')
#                     if not qualified_name:
#                         qualified_name = f"composio.{custom_mcp['name'].replace(' ', '_').lower()}"

#                     mcp_config = {
#                         'name': custom_mcp['name'],
#                         'qualifiedName': qualified_name,
#                         'config': custom_mcp.get('config', {}),
#                         'enabledTools': custom_mcp.get('enabledTools', []),
#                         'instructions': custom_mcp.get('instructions', ''),
#                         'isCustom': True,
#                         'customType': 'composio'
#                     }
#                     all_mcps.append(mcp_config)
#                     continue

#                 mcp_config = {
#                     'name': custom_mcp['name'],
#                     'qualifiedName': f"custom_{custom_type}_{custom_mcp['name'].replace(' ', '_').lower()}",
#                     'config': custom_mcp['config'],
#                     'enabledTools': custom_mcp.get('enabledTools', []),
#                     'instructions': custom_mcp.get('instructions', ''),
#                     'isCustom': True,
#                     'customType': custom_type
#                 }
#                 all_mcps.append(mcp_config)

#         if not all_mcps:
#             return None

#         mcp_wrapper_instance = MCPToolWrapper(mcp_configs=all_mcps)
#         try:
#             await mcp_wrapper_instance.initialize_and_register_tools()

#             updated_schemas = mcp_wrapper_instance.get_schemas()
#             for method_name, schema_list in updated_schemas.items():
#                 for schema in schema_list:
#                     self.thread_manager.tool_registry.tools[method_name] = {
#                         "instance": mcp_wrapper_instance,
#                         "schema": schema
#                     }

#             logger.info(f"⚡ Registered {len(updated_schemas)} MCP tools (Redis cache enabled)")
#             return mcp_wrapper_instance
#         except Exception as e:
#             logger.error(f"Failed to initialize MCP tools: {e}")
#             return None


class PromptManager:
    @staticmethod
    # async def build_system_prompt(model_name: str, agent_config: Optional[dict],
    #                               is_agent_builder: bool, thread_id: str,
    #                               mcp_wrapper_instance: Optional[MCPToolWrapper]) -> dict:
    async def build_system_prompt(
        model_name: str,
        agent_config: Optional[dict],
        is_agent_builder: bool,
        thread_id: str,
    ) -> dict:

        # 可以根据不同模型的特性，添加不同的系统提示词
        if (
            "gemini-2.5-flash" in model_name.lower()
            and "gemini-2.5-pro" not in model_name.lower()
        ):
            default_system_content = get_gemini_system_prompt()
        else:
            default_system_content = get_system_prompt()

        system_content = default_system_content

        now = datetime.datetime.now(datetime.timezone.utc)
        datetime_info = f"\n\n=== CURRENT DATE/TIME INFORMATION ===\n"
        datetime_info += f"Today's date: {now.strftime('%A, %B %d, %Y')}\n"
        datetime_info += f"Current UTC time: {now.strftime('%H:%M:%S UTC')}\n"
        datetime_info += f"Current year: {now.strftime('%Y')}\n"
        datetime_info += f"Current month: {now.strftime('%B')}\n"
        datetime_info += f"Current day: {now.strftime('%A')}\n"
        datetime_info += "Use this information for any time-sensitive tasks, research, or when current date/time context is needed.\n"

        system_content += datetime_info

        return {"role": "system", "content": system_content}


class MessageManager:
    """
    消息管理器类

    负责构建临时消息，包括浏览器状态和图像上下文信息。
    这些临时消息会在AI处理用户请求时作为上下文信息提供给模型。
    """

    def __init__(self, client, thread_id: str, model_name: str, trace: Optional[StatefulTraceClient]):  # type: ignore
        """
        初始化消息管理器

        Args:
            client: 数据库客户端，用于查询消息表
            thread_id: 线程ID，用于标识特定的对话线程
            model_name: 模型名称，用于判断是否支持图像处理
            trace: 追踪客户端，用于日志记录
        """
        self.client = client
        self.thread_id = thread_id
        self.model_name = model_name
        self.trace = trace

    async def build_temporary_message(self) -> Optional[dict]:
        """
        构建临时消息

        这个方法会：
        1. 获取最新的浏览器状态信息（包括截图）
        2. 获取最新的图像上下文信息
        3. 将这些信息组合成一个临时消息，供AI模型使用

        Returns:
            Optional[dict]: 包含浏览器状态和图像信息的临时消息，如果没有相关信息则返回None
        """
        temp_message_content_list = []  # 存储临时消息的内容列表

        # 获取最新的浏览器状态消息
        latest_browser_state_msg = (
            await self.client.table("messages")
            .select("*")
            .eq("thread_id", self.thread_id)
            .eq("type", "browser_state")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if latest_browser_state_msg.data and len(latest_browser_state_msg.data) > 0:
            try:
                # 解析浏览器状态内容
                browser_content = latest_browser_state_msg.data[0]["content"]
                if isinstance(browser_content, str):
                    browser_content = json.loads(browser_content)

                # 提取截图信息
                screenshot_base64 = browser_content.get(
                    "screenshot_base64"
                )  # Base64编码的截图
                screenshot_url = browser_content.get("base64_data")  # 截图的base64数据

                # 复制浏览器状态文本，移除截图相关字段
                browser_state_text = browser_content.copy()
                browser_state_text.pop("screenshot_base64", None)
                browser_state_text.pop("base64_data", None)

                # 如果有浏览器状态文本信息，添加到临时消息中
                if browser_state_text:
                    temp_message_content_list.append(
                        {
                            "type": "text",
                            "text": f"The following is the current state of the browser:\n{json.dumps(browser_state_text, indent=2)}",
                        }
                    )

                # 检查模型是否支持图像处理（Gemini、Anthropic、OpenAI）
                if (
                    "gemini" in self.model_name.lower()
                    or "anthropic" in self.model_name.lower()
                    or "openai" in self.model_name.lower()
                ):
                    # 优先使用URL，如果没有则使用Base64
                    if screenshot_url:
                        temp_message_content_list.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": screenshot_url,
                                    "format": "image/jpeg",
                                },
                            }
                        )
                    elif screenshot_base64:
                        temp_message_content_list.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{screenshot_base64}",
                                },
                            }
                        )

            except Exception as e:
                logger.error(f"Error parsing browser state: {e}")

        # 获取最新的图像上下文消息
        latest_image_context_msg = (
            await self.client.table("messages")
            .select("*")
            .eq("thread_id", self.thread_id)
            .eq("type", "image_context")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if latest_image_context_msg.data and len(latest_image_context_msg.data) > 0:
            try:
                # 解析图像上下文内容
                image_context_content = (
                    latest_image_context_msg.data[0]["content"]
                    if isinstance(latest_image_context_msg.data[0]["content"], dict)
                    else json.loads(latest_image_context_msg.data[0]["content"])
                )

                # 提取图像信息
                base64_image = image_context_content.get("base64")  # Base64编码的图像
                mime_type = image_context_content.get("mime_type")  # 图像的MIME类型
                file_path = image_context_content.get(
                    "file_path", "unknown file"
                )  # 图像文件路径

                # 如果有图像数据，添加到临时消息中
                if base64_image and mime_type:
                    # 添加图像描述文本
                    temp_message_content_list.append(
                        {
                            "type": "text",
                            "text": f"Here is the image you requested to see: '{file_path}'",
                        }
                    )
                    # 添加图像URL
                    temp_message_content_list.append(
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime_type};base64,{base64_image}",
                            },
                        }
                    )

                # 处理完图像上下文后，删除该消息（避免重复使用）
                await self.client.table("messages").delete().eq(
                    "message_id", latest_image_context_msg.data[0]["message_id"]
                ).execute()

            except Exception as e:
                logger.error(f"Error parsing image context: {e}")

        # 如果有临时消息内容，返回格式化的消息
        if temp_message_content_list:
            return {"role": "user", "content": temp_message_content_list}
        return None


class AgentRunner:
    """
    Agent Runner using AgentScope framework.

    This class orchestrates the agent execution using AgentScope's
    Orchestrator-Worker architecture instead of Google ADK.
    """

    def __init__(self, config: AgentConfig):
        self.config = config
        self.agentscope_runner = None
        self.client = None
        self.account_id = None

    async def setup(self):
        """Initialize database connection and AgentScope runner."""
        try:
            # Setup Langfuse trace (v3: root is a span; trace-level attrs set via update_trace)
            if not self.config.trace:
                self.config.trace = langfuse.start_span(name="run_agent")
                try:
                    self.config.trace.update_trace(
                        session_id=self.config.thread_id,
                        metadata={"project_id": self.config.project_id},
                    )
                except Exception as _e:
                    logger.warning(f"Langfuse update_trace failed: {_e}")
                logger.info(f"Langfuse trace created successfully")
            else:
                logger.info(f"Using existing trace")

            # Initialize database connection
            from services.postgresql import DBConnection

            db = DBConnection()
            self.client = await db.client
            logger.info(f"Database client initialized successfully")

            # Get account ID
            from utils.auth_utils import AuthUtils

            self.account_id = await AuthUtils.get_account_id_from_thread(
                self.client, self.config.thread_id
            )
            if not self.account_id:
                raise ValueError("Could not determine account ID for thread")

            # Get project info
            project = (
                await self.client.table("projects")
                .select("*")
                .eq("project_id", self.config.project_id)
                .execute()
            )
            if not project.data or len(project.data) == 0:
                raise ValueError(f"Project {self.config.project_id} not found")

            project_data = project.data[0]
            sandbox_info = project_data.get("sandbox", {})

            # Handle sandbox_info being a string
            if isinstance(sandbox_info, str):
                try:
                    sandbox_info = json.loads(sandbox_info)
                except (json.JSONDecodeError, TypeError):
                    sandbox_info = {}

            if not sandbox_info.get("id"):
                logger.info(
                    f"No sandbox found for project {self.config.project_id}; will create lazily when needed"
                )

            agent_backend = os.getenv("AGENT_BACKEND", "")
            allow_claude_sdk_backend = os.getenv(
                "ALLOW_CLAUDE_SDK_BACKEND",
                "",
            ).strip().lower() in ("1", "true", "yes")
            explicit_claude_sdk_backend = _agent_config_requests_claude_sdk_backend(
                self.config.agent_config
            )
            if (
                _is_claude_sdk_backend(agent_backend) or explicit_claude_sdk_backend
            ) and allow_claude_sdk_backend:
                from agentscope_integration.prompts.orchestrator_prompt import (
                    get_orchestrator_prompt,
                )

                orchestrator_prompt = get_orchestrator_prompt()
                configured_system_prompt = ""
                if isinstance(self.config.agent_config, dict):
                    configured_system_prompt = str(
                        self.config.agent_config.get("system_prompt") or ""
                    ).strip()
                claude_sdk_system_prompt = (
                    f"{configured_system_prompt}\n\n{orchestrator_prompt}"
                    if configured_system_prompt
                    else orchestrator_prompt
                )

                self.agentscope_runner = ClaudeSDKRunner(
                    thread_id=self.config.thread_id,
                    project_id=self.config.project_id,
                    model_key="claude-sdk",
                    db_client=self.client,
                    trace=self.config.trace,
                    system_prompt=claude_sdk_system_prompt,
                )
                logger.info("ClaudeSDKRunner created successfully")
            elif self.config.shadow_clone_mode == ShadowCloneMode.OFF:
                model_key = self._map_model_name(self.config.model_name)
                runtime_reasoning_effort = (
                    self.config.reasoning_effort
                    if self.config.enable_thinking
                    else None
                )
                self.agentscope_runner = AgentScopeRunner(
                    thread_id=self.config.thread_id,
                    project_id=self.config.project_id,
                    model_key=model_key,
                    reasoning_effort=runtime_reasoning_effort,
                    db_client=self.client,
                    trace=self.config.trace,
                )
                logger.info(
                    f"AgentScopeRunner created successfully with model: {model_key}"
                )
            else:
                if not self.config.agent_run_id:
                    raise ValueError(
                        "agent_run_id is required when Shadow Clone mode is enabled"
                    )
                shadow_clone_main_model = (
                    self.config.shadow_clone_main_model
                    if self.config.shadow_clone_main_model
                    else self.config.model_name
                )
                model_key = self._map_model_name(shadow_clone_main_model)
                from agentscope_integration.shadow_clone_v2.runner import (
                    ShadowCloneV2Runner,
                )

                self.agentscope_runner = ShadowCloneV2Runner(
                    thread_id=self.config.thread_id,
                    project_id=self.config.project_id,
                    account_id=self.account_id,
                    agent_run_id=self.config.agent_run_id,
                    model_key=model_key,
                    db_client=self.client,
                    mode=self.config.shadow_clone_mode,
                    subagent_model=self.config.shadow_clone_subagent_model,
                    trace=self.config.trace,
                )
                logger.info(
                    "ShadowCloneV2Runner created successfully with model=%s mode=%s run_id=%s subagent_model=%s",
                    model_key,
                    self.config.shadow_clone_mode.value,
                    self.config.agent_run_id,
                    self.config.shadow_clone_subagent_model,
                )

        except Exception as setup_error:
            logger.error(f"Error details: {traceback.format_exc()}")
            raise setup_error

    def _map_model_name(self, model_name: str) -> str:
        """Map OpenRouter/LiteLLM model name to AgentScope model key."""
        normalized_name = str(model_name or "").strip()
        if not normalized_name:
            raise ValueError("Model name is empty")

        model_mapping = {
            # OpenRouter format (from frontend) - with openrouter/ prefix
            "openrouter/google/gemini-3.1-pro-preview": "gemini-3-pro",
            "openrouter/google/gemini-3-flash-preview": "gemini-3-flash",
            "openrouter/minimax/minimax-m2.1": "minimax-m2.1",
            "openrouter/minimax/minimax-m2.5": "openrouter-minimax-m2.5",
            "openrouter/minimax/minimax-m2.7": "openrouter-minimax-m2.7",
            "openrouter/xiaomi/mimo-v2-pro": "mimo-v2-pro",
            "openrouter/xiaomi/mimo-v2.5-pro": "mimo-v2.5-pro",
            "openrouter/moonshotai/kimi-k2.5": "openrouter-kimi-k2.5",
            "openrouter/moonshotai/kimi-k2.6": "openrouter-kimi-k2.6",
            "openrouter/deepseek/deepseek-v4-pro": "openrouter-deepseek-v4-pro",
            "openrouter/deepseek/deepseek-v4-flash": "openrouter-deepseek-v4-flash",
            "ppio/moonshotai/kimi-k2.5": "ppio-kimi-k2.5",
            "openrouter/z-ai/glm-4.7": "glm-4.7",
            "openrouter/z-ai/glm-5.1": "glm-5.1",
            "openrouter/z-ai/glm-5": "glm-5",
            "openrouter/qwen/qwen3.6-plus": "qwen3.6-plus",
            "openrouter/qwen/qwen3.5-plus": "qwen3.5-plus",
            "dashscope/qwen3.5-plus": "qwen3.5-plus",
            "openrouter/qwen/qwen3.5-397b-a17b": "qwen3.5-plus",
            "dashscope/qwen3.5-397b-a17b": "qwen3.5-plus",
            "dashscope/minimax-m2.5": "dashscope-minimax-m2.5",
            "openrouter/anthropic/claude-sonnet-4.5": "claude-sonnet-4.5",
            "volcengine/doubao-seed-2-0-pro-260215": "doubao-seed-2-0-pro-260215",
            "volcengine/doubao-seed-2-0-code-preview-260215": "doubao-seed-2-0-code-preview-260215",
            "openai-proxy/gpt-5.4": "proxy-gpt-5.4",
            "mystery-model": "proxy-gpt-5.4",
            # Provider-native format
            "google/gemini-3.1-pro-preview": "gemini-3-pro",
            "google/gemini-3-flash-preview": "gemini-3-flash",
            "minimax/minimax-m2.1": "minimax-m2.1",
            "minimax/minimax-m2.5": "openrouter-minimax-m2.5",
            "minimax/minimax-m2.7": "openrouter-minimax-m2.7",
            "xiaomi/mimo-v2-pro": "mimo-v2-pro",
            "xiaomi/mimo-v2.5-pro": "mimo-v2.5-pro",
            "MiniMax-M2.5": "dashscope-minimax-m2.5",
            "moonshotai/kimi-k2.5": "kimi-k2.5",
            "moonshotai/kimi-k2.6": "kimi-k2.6",
            "kimi-k2.5": "kimi-k2.5",
            "kimi-k2.6": "kimi-k2.6",
            "anthropic/claude-sonnet-4.5": "claude-sonnet-4.5",
            "deepseek/deepseek-chat": "kimi-k2.5",
            "deepseek/deepseek-v4-pro": "deepseek-v4-pro-high",
            "deepseek/deepseek-v4-flash": "deepseek-v4-flash-high",
            "deepseek-v4-pro-high": "deepseek-v4-pro-high",
            "deepseek-v4-pro-max": "deepseek-v4-pro-max",
            "deepseek-v4-flash-high": "deepseek-v4-flash-high",
            "deepseek-v4-flash-max": "deepseek-v4-flash-max",
            "z-ai/glm-4.7": "glm-4.7",
            "glm-5.1": "glm-5.1",
            "z-ai/glm-5.1": "glm-5.1",
            "z-ai/glm-5": "glm-5",
            "qwen/qwen3.6-plus": "qwen3.6-plus",
            "qwen/qwen3.5-plus": "qwen3.5-plus",
            "qwen/qwen3.5-397b-a17b": "qwen3.5-plus",
            "qwen3.6-plus": "qwen3.6-plus",
            "qwen3.5-plus": "qwen3.5-plus",
            "qwen3.5-397b-a17b": "qwen3.5-plus",
            "doubao-seed-2-0-pro-260215": "doubao-seed-2-0-pro-260215",
            "doubao-seed-2-0-code-preview-260215": "doubao-seed-2-0-code-preview-260215",
            "gpt-5.4": "proxy-gpt-5.4",
            # LiteLLM format (legacy)
            "gemini/gemini-3.1-pro-preview": "gemini-3-pro",
            "gemini/gemini-3-flash-preview": "gemini-3-flash",
            "gemini/gemini-2.5-pro-preview-05-06": "gemini-3-pro",
            "gemini/gemini-2.5-flash-preview-05-20": "gemini-3-flash",
            "moonshot/moonshot-v1-250k": "kimi-k2.5",
            "kimi/kimi-k2.5": "kimi-k2.5",
            "claude-sonnet-4.5": "claude-sonnet-4.5",
            # DeepSeek direct
            "deepseek-chat": "kimi-k2.5",
        }

        resolution_reason = ""

        # Try exact match first
        if normalized_name in model_mapping:
            resolved_key = model_mapping[normalized_name]
            resolution_reason = "exact_match"

        # Try partial match
        if not resolution_reason:
            model_lower = normalized_name.lower()
            if "gemini-3.1-pro" in model_lower or "gemini-2.5-pro" in model_lower:
                resolved_key = "gemini-3-pro"
                resolution_reason = "gemini_pro_partial_match"
            elif "gemini-3-flash" in model_lower or "gemini-2.5-flash" in model_lower:
                resolved_key = "gemini-3-flash"
                resolution_reason = "gemini_flash_partial_match"
            elif "ppio" in model_lower and "kimi-k2.5" in model_lower:
                resolved_key = "ppio-kimi-k2.5"
                resolution_reason = "ppio_kimi_partial_match"
            elif "openrouter" in model_lower and "kimi-k2.5" in model_lower:
                resolved_key = "openrouter-kimi-k2.5"
                resolution_reason = "openrouter_kimi_partial_match"
            elif "openrouter" in model_lower and "kimi-k2.6" in model_lower:
                resolved_key = "openrouter-kimi-k2.6"
                resolution_reason = "openrouter_kimi26_partial_match"
            elif "deepseek-v4-pro" in model_lower:
                resolved_key = (
                    "openrouter-deepseek-v4-pro"
                    if "openrouter" in model_lower or "/" in model_lower
                    else "deepseek-v4-pro-high"
                )
                resolution_reason = "deepseek_v4_pro_partial_match"
            elif "deepseek-v4-flash" in model_lower:
                resolved_key = (
                    "openrouter-deepseek-v4-flash"
                    if "openrouter" in model_lower or "/" in model_lower
                    else "deepseek-v4-flash-high"
                )
                resolution_reason = "deepseek_v4_flash_partial_match"
            elif "deepseek" in model_lower:
                resolved_key = "kimi-k2.5"
                resolution_reason = "deepseek_compat_match"
            elif "doubao-seed-2-0-code-preview-260215" in model_lower:
                resolved_key = "doubao-seed-2-0-code-preview-260215"
                resolution_reason = "doubao_code_partial_match"
            elif "doubao-seed-2-0-pro-260215" in model_lower:
                resolved_key = "doubao-seed-2-0-pro-260215"
                resolution_reason = "doubao_pro_partial_match"
            elif "glm-5.1" in model_lower:
                resolved_key = "glm-5.1"
                resolution_reason = "glm51_partial_match"
            elif "glm-5" in model_lower:
                resolved_key = "glm-5"
                resolution_reason = "glm5_partial_match"
            elif "glm-4.7" in model_lower:
                resolved_key = "glm-4.7"
                resolution_reason = "glm47_partial_match"
            elif "mimo-v2-pro" in model_lower:
                resolved_key = "mimo-v2-pro"
                resolution_reason = "mimo_v2_pro_partial_match"
            elif "mimo-v2.5-pro" in model_lower:
                resolved_key = "mimo-v2.5-pro"
                resolution_reason = "mimo_v25_pro_partial_match"
            elif "qwen3.6-plus" in model_lower:
                resolved_key = "qwen3.6-plus"
                resolution_reason = "qwen36_plus_partial_match"
            elif "qwen3.5-plus" in model_lower:
                resolved_key = "qwen3.5-plus"
                resolution_reason = "qwen35_plus_partial_match"
            elif "qwen3.5-397b-a17b" in model_lower:
                resolved_key = "qwen3.5-plus"
                resolution_reason = "qwen35_legacy_partial_match"
            elif "qwen" in model_lower:
                # Keep Qwen alias handling permissive so provider-specific names
                # such as ollama_chat/qwen3:* still route to the DashScope key.
                resolved_key = "qwen3.5-plus"
                resolution_reason = "qwen_alias_fallback"
            elif (
                "openrouter" in model_lower
                and "minimax" in model_lower
                and "m2.7" in model_lower
            ):
                resolved_key = "openrouter-minimax-m2.7"
                resolution_reason = "openrouter_minimax_m27_partial_match"
            elif (
                "openrouter" in model_lower
                and "minimax" in model_lower
                and "m2.5" in model_lower
            ):
                resolved_key = "openrouter-minimax-m2.5"
                resolution_reason = "openrouter_minimax_m25_partial_match"
            elif (
                "dashscope" in model_lower
                and "minimax" in model_lower
                and "m2.5" in model_lower
            ):
                resolved_key = "dashscope-minimax-m2.5"
                resolution_reason = "dashscope_minimax_m25_partial_match"
            elif "minimax" in model_lower and "m2.7" in model_lower:
                resolved_key = "openrouter-minimax-m2.7"
                resolution_reason = "minimax_m27_partial_match"
            elif "minimax" in model_lower and "m2.5" in model_lower:
                resolved_key = "minimax-m2.5"
                resolution_reason = "minimax_m25_partial_match"
            elif "minimax" in model_lower and "m2.1" in model_lower:
                resolved_key = "minimax-m2.1"
                resolution_reason = "minimax_m21_partial_match"
            elif "openai-proxy" in model_lower and "gpt-5.4" in model_lower:
                resolved_key = "proxy-gpt-5.4"
                resolution_reason = "openai_proxy_gpt54_partial_match"
            elif model_lower == "mystery-model" or model_lower == "gpt-5.4":
                resolved_key = "proxy-gpt-5.4"
                resolution_reason = "gpt54_alias_match"
            elif "kimi" in model_lower or "moonshot" in model_lower:
                resolved_key = "kimi-k2.5"
                resolution_reason = "kimi_moonshot_partial_match"

        if not resolution_reason:
            supported_models = sorted(set(model_mapping.keys()))
            preview = ", ".join(supported_models[:12])
            raise ValueError(
                "Unknown model name '{model}'. Supported examples: {examples}".format(
                    model=normalized_name,
                    examples=preview,
                )
            )

        logger.info(
            "[AgentRunner] Model resolution: raw_model_name=%s "
            "resolved_model_key=%s resolution_reason=%s",
            normalized_name,
            resolved_key,
            resolution_reason,
        )
        return resolved_key

    async def run(self) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Run the agent using AgentScope framework.

        This method:
        1. Sets up the AgentScope runner
        2. Extracts the user message from events table
        3. Runs the agent and yields SSE-formatted streaming output
        """
        await self.setup()

        # Use the agent_run_id from config (set by run_agent_background)
        # so workspace artifacts are linked to the correct agent run.
        thread_run_id = self.config.agent_run_id or str(uuid4())

        # Get latest user message from events table
        latest_user_message = (
            await self.client.table("events")
            .select("*")
            .eq("session_id", self.config.thread_id)
            .order("timestamp", desc=True)
            .limit(10)
            .execute()
        )

        # Extract user request content (text + optional media refs)
        user_request = None
        user_media_refs: List[Dict[str, str]] = []
        if latest_user_message.data and len(latest_user_message.data) > 0:
            for event in latest_user_message.data:
                if event.get("author") == "user":
                    content = event.get("content", {})

                    # Parse content field
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except json.JSONDecodeError:
                            content = {"content": content}

                    payload = parse_user_event_content(content)
                    user_request, user_media_refs = payload_to_runner_input(payload)
                    if user_request or user_media_refs:
                        if user_request:
                            logger.info(
                                f"Extracted user request: {user_request[:100]}..."
                            )
                        logger.info(
                            "[AgentRunner] Extracted %d media refs from latest user event",
                            len(user_media_refs),
                        )
                        break

            if self.config.trace and (user_request or user_media_refs):
                trace_input = (
                    user_request
                    or f"[user_input_with_{len(user_media_refs)}_media_refs]"
                )
                self.config.trace.update(input=trace_input)

        if not user_request and user_media_refs:
            user_request = "Please analyze the uploaded image(s)."

        if self.config.user_message_override:
            override_message = self.config.user_message_override.strip()
            if override_message:
                base_request = (user_request or "").strip()
                user_request = (
                    f"{base_request}\n\n{override_message}"
                    if base_request
                    else override_message
                )
                logger.info(
                    "[AgentRunner] Applied user message override for retry flow "
                    "(thread=%s override_chars=%s)",
                    self.config.thread_id,
                    len(override_message),
                )

        if not user_request:
            yield {
                "type": "status",
                "status": "error",
                "message": "No user message found",
            }
            return

        user_request = _augment_user_message_with_multi_agent_mode(
            user_request,
            self.config.shadow_clone_mode,
        )

        # Create Langfuse generation for tracking, linked to orchestrator-system prompt
        prompt_obj = None
        try:
            from agentscope_integration.prompts.orchestrator_prompt import (
                get_orchestrator_prompt_object,
            )

            prompt_obj = get_orchestrator_prompt_object()
        except Exception as _e:
            logger.warning(
                f"[AgentRunner] Failed to fetch orchestrator prompt object for trace link: {_e}"
            )

        if self.config.trace:
            gen_kwargs = {"name": "agentscope_runner.run", "as_type": "generation"}
            if prompt_obj is not None:
                gen_kwargs["prompt"] = prompt_obj
            generation = self.config.trace.start_observation(**gen_kwargs)
        else:
            generation = None

        try:
            logger.info(
                f"[AgentRunner] Starting AgentScope run for thread {self.config.thread_id}"
            )

            # Run AgentScope and yield streaming output
            _chunks_for_output: list[dict[str, Any]] = []
            async for chunk in self.agentscope_runner.run(
                user_message=user_request,
                user_media_refs=user_media_refs,
                thread_run_id=thread_run_id,
                resume_strategy=self.config.resume_strategy,
                resume_window_minutes=self.config.resume_window_minutes,
            ):
                if isinstance(chunk, dict):
                    _chunks_for_output.append(chunk)
                yield chunk

            logger.info(
                f"[AgentRunner] AgentScope run completed for thread {self.config.thread_id}"
            )

            if generation:
                generation.update(
                    output="AgentScope run completed", status_message="success"
                )
                generation.end()

            # Write agent output to trace for LLM-as-Judge evaluators.
            # Use BOTH update_trace (trace-level) and update (root-span-level)
            # because Langfuse v3 + ClickHouse may read output from either.
            if self.config.trace is not None and _chunks_for_output:
                try:
                    final_output = _extract_chunks_output_text(_chunks_for_output)
                    if final_output:
                        self.config.trace.update_trace(output=final_output)
                        self.config.trace.update(output=final_output)
                except Exception:
                    pass

        except Exception as e:
            error_msg = f"Error running AgentScope: {str(e)}"
            logger.error(error_msg)
            logger.error(f"Error details: {traceback.format_exc()}")

            if generation:
                generation.update(
                    output=error_msg, status_message="error", level="ERROR"
                )
                generation.end()

            yield {"type": "status", "status": "error", "message": error_msg}
        finally:
            if self.agentscope_runner and hasattr(self.agentscope_runner, "close"):
                try:
                    await self.agentscope_runner.close()
                except Exception as close_exc:
                    logger.warning(
                        "[AgentRunner] Failed to close AgentScope sidecar resources: %s",
                        close_exc,
                    )

        # Flush Langfuse
        asyncio.create_task(asyncio.to_thread(lambda: langfuse.flush()))


def _extract_chunks_output_text(
    chunks: list[dict[str, Any]],
    *,
    max_chars: int = 50_000,
) -> str:
    """Extract concatenated assistant LLM text from SSE-style response chunks.

    Each chunk is a dict with ``type``, ``is_llm_message``, and ``content``
    (a JSON-serialised dict whose ``content`` key holds the display text).
    """
    text_parts: list[str] = []
    for chunk in chunks:
        if chunk.get("type") != "assistant":
            continue
        if not chunk.get("is_llm_message"):
            continue
        try:
            raw = chunk.get("content")
            if isinstance(raw, str):
                inner = json.loads(raw)
            elif isinstance(raw, dict):
                inner = raw
            else:
                continue
            text = inner.get("content", "") if isinstance(inner, dict) else ""
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())

    if not text_parts:
        return ""

    full_text = "\n\n".join(text_parts)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
    return full_text


async def run_agent(
    thread_id: str,
    project_id: str,
    stream: bool,
    thread_manager=None,  # Deprecated, kept for backward compatibility
    native_max_auto_continues: int = 0,
    max_iterations: int = 100,
    model_name: str = config.MODEL_TO_USE or "gemini/gemini-3.1-pro-preview",
    enable_thinking: Optional[bool] = False,
    reasoning_effort: Optional[str] = "low",
    enable_context_manager: bool = True,
    agent_config: Optional[dict] = None,
    trace: Optional[StatefulTraceClient] = None,  # type: ignore
    is_agent_builder: Optional[bool] = False,
    target_agent_id: Optional[str] = None,
    resume_strategy: str = "auto",
    resume_window_minutes: int = 1440,
    user_message_override: Optional[str] = None,
    agent_run_id: Optional[str] = None,
    shadow_clone_mode: ShadowCloneMode = ShadowCloneMode.OFF,
    shadow_clone_main_model: Optional[str] = None,
    shadow_clone_subagent_model: Optional[str] = None,
):
    # max_iterations - 外层循环（Agent级别）：每次迭代 = 一轮完整的"思考 → 调用工具 → 处理结果"，用于防止 Agent 陷入无限循环
    # native_max_auto_continues - 内层循环（LLM级别）：当 LLM 返回 finish_reason='length'（未完成）或其他原因导致的意外终止时，自动继续生成
    config = AgentConfig(
        thread_id=thread_id,
        project_id=project_id,
        stream=stream,
        native_max_auto_continues=native_max_auto_continues,  # 控制 AI Agent 自动继续对话的最大次数
        max_iterations=max_iterations,  # Agent 最大迭代次数
        model_name=model_name,
        enable_thinking=enable_thinking,  # 是否启用思考
        reasoning_effort=reasoning_effort,  # 思考力度
        enable_context_manager=enable_context_manager,
        agent_config=agent_config,  # Agent 配置
        trace=trace,
        is_agent_builder=is_agent_builder,  # 是否是 Agent 构建器（）
        target_agent_id=target_agent_id,  # 目标 Agent ID
        resume_strategy=resume_strategy,
        resume_window_minutes=resume_window_minutes,
        user_message_override=user_message_override,
        agent_run_id=agent_run_id,
        shadow_clone_mode=shadow_clone_mode,
        shadow_clone_main_model=shadow_clone_main_model,
        shadow_clone_subagent_model=shadow_clone_subagent_model,
    )

    # 创建 Runner
    runner = AgentRunner(config)
    logger.info(f"AgentRunner created successfully: {runner}")

    try:
        logger.info(f"Starting to run runner.run()")
        async for chunk in runner.run():
            yield chunk
    except Exception as run_error:
        logger.error(f"runner.run() failed: {run_error}")
        logger.error(f"Error details: {traceback.format_exc()}")
        raise run_error
