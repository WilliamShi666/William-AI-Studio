"""
模型名称和配置解析工具

统一处理模型名称解析、API Key 和 API Base URL 的逻辑
"""

import os
import logging
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
from utils.config import config
from utils.constants import MODEL_NAME_ALIASES

logger = logging.getLogger(__name__)

# 确保加载环境变量 - .env 文件在 backend 目录
current_dir = Path(__file__).resolve().parent  # utils 目录
backend_dir = current_dir.parent  # backend 目录
env_file = backend_dir / ".env"  # .env 文件在 backend 目录

if env_file.exists():
    load_dotenv(dotenv_path=env_file, override=False)

else:
    logger.warning(f".env file not found at: {env_file}")
    # 尝试从当前目录加载
    load_dotenv(override=False)


class ModelConfig:
    """模型配置数据类"""

    def __init__(
        self,
        model_name: str,
        api_key: Optional[str] = None,
        api_base: Optional[str] = None,
        provider: str = "Unknown",
        base_url: Optional[str] = None,  # 专门用于 Ollama 的 base_url
    ):
        self.model_name = model_name
        self.api_key = api_key
        self.api_base = api_base
        self.provider = provider
        self.base_url = base_url  # Ollama 使用


def resolve_model_config(model_name: Optional[str] = None) -> ModelConfig:
    """
    解析模型配置，统一处理模型名称、API Key 和 API Base

    Args:
        model_name: 原始模型名称，如果为 None 则使用默认模型

    Returns:
        ModelConfig: 包含完整模型配置的对象

    Raises:
        ValueError: 当必需的配置缺失时
    """
    # 1. 如果没有提供模型名称，使用默认模型
    if model_name is None:
        model_name = config.MODEL_TO_USE
        logger.info(f"No model name provided, using default model: {model_name}")

    # 2. 特殊处理：Ollama 模型
    if model_name == "ollama":
        return _resolve_ollama_config()

    # TODO: 3. 其他特殊模型处理（vllm、SGLang 等）
    # if model_name == "vllm":
    #     return _resolve_vllm_config()

    # 4. 处理模型名称别名映射（跳过已经特殊处理过的模型）
    if not model_name.startswith("ollama_chat/"):
        resolved_model = MODEL_NAME_ALIASES.get(model_name, model_name)
        if resolved_model != model_name:
            logger.info(f"Model alias mapping: {model_name} -> {resolved_model}")
        model_name = resolved_model

    # 5. 特殊处理 DeepSeek 模型格式
    if "DeepSeek" in model_name and "/" in model_name:
        logger.warning(f"Detected uppercase DeepSeek format: {model_name}, converting to standard format")
        model_name = "deepseek/deepseek-chat"
        logger.info(f"Converted to: {model_name}")

    if str(model_name or "").strip().lower() == "gpt-5.4":
        model_name = "openai-proxy/gpt-5.4"
        logger.info("Normalized bare gpt-5.4 to OpenAI proxy route")

    # 6. 根据模型名称确定提供商并获取 API Key 和 API Base
    api_key = None
    api_base = None
    provider = "Unknown"
    model_name_lower = model_name.lower()

    # OpenRouter must be detected before provider-specific substrings.
    if model_name_lower.startswith("openrouter/"):
        api_key = config.OPENROUTER_API_KEY or config.OPENAI_API_KEY
        if config.OPENROUTER_API_KEY is None and config.OPENAI_API_KEY:
            logger.warning(
                "OPENROUTER_API_KEY not set; falling back to OPENAI_API_KEY for OpenRouter.",
            )

        env_api_base = os.getenv("OPENROUTER_API_BASE")
        env_base_url = os.getenv("OPENROUTER_BASE_URL")
        api_base = env_api_base or env_base_url or config.OPENROUTER_API_BASE
        provider = "OpenRouter"

    elif model_name_lower.startswith("openai-proxy/"):
        api_key = getattr(config, "OPENAI_PROXY_API_KEY", None) or os.getenv(
            "OPENAI_PROXY_API_KEY",
        )
        env_api_base = os.getenv("OPENAI_PROXY_API_BASE")
        env_base_url = os.getenv("OPENAI_PROXY_BASE_URL")
        api_base = (
            env_api_base
            or env_base_url
            or getattr(config, "OPENAI_PROXY_BASE_URL", None)
            or "https://api.openai-proxy.org/v1"
        )
        provider = "OpenAI Proxy"

    elif model_name_lower.startswith("ppio/"):
        api_key = getattr(config, "PPIO_API_KEY", None) or os.getenv("PPIO_API_KEY")
        env_api_base = os.getenv("PPIO_API_BASE")
        env_base_url = os.getenv("PPIO_BASE_URL")
        api_base = (
            env_api_base
            or env_base_url
            or getattr(config, "PPIO_API_BASE", None)
            or getattr(config, "PPIO_BASE_URL", None)
            or "https://api.ppio.com/openai"
        )
        provider = "PPIO"

    elif model_name_lower.startswith("dashscope/minimax-m2.5"):
        api_key = getattr(config, "DASHSCOPE_API_KEY", None)
        env_api_base = os.getenv("DASHSCOPE_COMPAT_BASE_URL")
        env_alt_api_base = os.getenv("DASHSCOPE_COMPATIBLE_BASE_URL")
        env_base_url = os.getenv("DASHSCOPE_API_BASE")
        api_base = (
            env_api_base
            or env_alt_api_base
            or (
                env_base_url
                if env_base_url and "compatible-mode" in env_base_url
                else None
            )
            or getattr(config, "DASHSCOPE_COMPATIBLE_BASE_URL", None)
            or "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        provider = "DashScope Compatible"

    elif (
        model_name_lower.startswith("dashscope/")
        or model_name_lower.startswith("qwen/qwen3.5-plus")
        or model_name_lower.startswith("qwen3.5-plus")
        or model_name_lower.startswith("qwen/qwen3.5-397b-a17b")
        or model_name_lower.startswith("qwen3.5-397b-a17b")
    ):
        api_key = getattr(config, "DASHSCOPE_API_KEY", None)
        env_native_base = os.getenv("DASHSCOPE_BASE_URL")
        env_api_base = os.getenv("DASHSCOPE_API_BASE")
        api_base = (
            env_native_base
            or (
                env_api_base
                if env_api_base and "compatible-mode" not in env_api_base
                else None
            )
            or getattr(config, "DASHSCOPE_BASE_URL", None)
            or getattr(config, "DASHSCOPE_API_BASE", None)
            or "https://dashscope.aliyuncs.com/api/v1"
        )
        provider = "DashScope"
    elif (
        model_name_lower.startswith("kimi-")
        or model_name_lower.startswith("moonshot/")
        or ("moonshot" in model_name_lower and not model_name_lower.startswith("openrouter/"))
    ):
        api_key = getattr(config, "MOONSHOT_API_KEY", None)
        env_api_base = os.getenv("MOONSHOT_API_BASE")
        env_base_url = os.getenv("MOONSHOT_BASE_URL")
        api_base = (
            env_api_base
            or env_base_url
            or getattr(config, "MOONSHOT_API_BASE", None)
            or getattr(config, "MOONSHOT_BASE_URL", None)
        )
        provider = "Moonshot"

    elif model_name_lower.startswith("volcengine/") or model_name_lower.startswith("doubao-"):
        api_key = (
            getattr(config, "VOLCENGINE_ARK_API_KEY", None)
            or getattr(config, "ARK_API_KEY", None)
            or os.getenv("VOLCENGINE_ARK_API_KEY")
            or os.getenv("ARK_API_KEY")
        )
        api_base = (
            os.getenv("VOLCENGINE_ARK_BASE_URL")
            or os.getenv("ARK_BASE_URL")
            or getattr(config, "VOLCENGINE_ARK_BASE_URL", None)
            or "https://ark.cn-beijing.volces.com/api/v3"
        )
        provider = "Volcengine Ark"

    elif "openai" in model_name_lower or "gpt" in model_name_lower:
        api_key = config.OPENAI_API_KEY
        provider = "OpenAI"

    elif "anthropic" in model_name_lower or "claude" in model_name_lower:
        api_key = config.ANTHROPIC_API_KEY
        provider = "Anthropic"

    elif "deepseek" in model_name_lower:
        # 优先使用 DEEPSEEK_API_KEY，回退到 OPENAI_API_KEY
        api_key = getattr(config, "DEEPSEEK_API_KEY", None) or config.OPENAI_API_KEY
        api_base = config.DEEPSEEK_API_BASE
        provider = (
            "DeepSeek"
            if getattr(config, "DEEPSEEK_API_KEY", None)
            else "DeepSeek (using OpenAI key)"
        )

    elif "gemini" in model_name_lower:
        api_key = config.GEMINI_API_KEY
        api_base = getattr(config, "GEMINI_API_BASE", None)
        provider = "Gemini"

    elif "ollama" in model_name_lower:
        api_key = config.OLLAMA_API_KEY
        provider = "Ollama"
        # 如果模型名包含 ollama_chat/ 前缀，从环境变量读取 base_url
        if model_name.startswith("ollama_chat/"):
            ollama_base_url = os.getenv("OLLAMA_BASE_URL")
            if ollama_base_url:
                return ModelConfig(
                    model_name=model_name,
                    api_key=api_key,
                    api_base=None,
                    provider=provider,
                    base_url=ollama_base_url,
                )
            else:
                logger.warning("OLLAMA_BASE_URL not found in environment variables")

    else:
        # 默认使用 OpenAI
        api_key = config.OPENAI_API_KEY
        provider = "OpenAI (default)"
        logger.warning(f"Unrecognized model {model_name}, using default OpenAI configuration")

    logger.info(f"Model resolved: {model_name}")
    logger.info(f"Provider: {provider}")
    logger.info(f"API Key: {'***' + api_key[-4:] if api_key else 'None'}")
    if api_base:
        logger.info(f"API Base: {api_base}")

    return ModelConfig(
        model_name=model_name,
        api_key=api_key,
        api_base=api_base,
        provider=provider,
    )


def _resolve_ollama_config() -> ModelConfig:
    """
    解析 Ollama 模型配置

    从环境变量读取 OLLAMA_MODEL_NAME 和 OLLAMA_BASE_URL，
    并拼接成 ollama_chat/{model_name} 格式

    Returns:
        ModelConfig: Ollama 模型配置

    Raises:
        ValueError: 当必需的环境变量缺失时
    """
    logger.info("🔍 Resolving Ollama configuration from environment variables")

    ollama_model_name = os.getenv("OLLAMA_MODEL_NAME")
    ollama_base_url = os.getenv("OLLAMA_BASE_URL")

    if not ollama_model_name:
        logger.error("OLLAMA_MODEL_NAME not found in environment variables")
        raise ValueError("OLLAMA_MODEL_NAME not configured")

    if not ollama_base_url:
        logger.error("OLLAMA_BASE_URL not found in environment variables")
        raise ValueError("OLLAMA_BASE_URL not configured")

    # 拼接成 ollama_chat 格式
    model_name = f"ollama_chat/{ollama_model_name}"

    result = ModelConfig(
        model_name=model_name,
        api_key=config.OLLAMA_API_KEY,
        api_base=None,
        provider="Ollama",
        base_url=ollama_base_url,  # Ollama 特有的 base_url
    )

    return result


# TODO: 未来可以添加其他模型提供商的解析函数
# def _resolve_vllm_config() -> ModelConfig:
#     """解析 vLLM 模型配置"""
#     pass
#
# def _resolve_sglang_config() -> ModelConfig:
#     """解析 SGLang 模型配置"""
#     pass
