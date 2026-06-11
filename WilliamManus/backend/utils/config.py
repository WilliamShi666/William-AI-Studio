"""
Configuration management.

This module provides a centralized way to access configuration settings and
environment variables across the application. It supports different environment
modes (development, staging, production) and provides validation for required
values.

Usage:
    from utils.config import config
    
    # Access configuration values
    api_key = config.OPENAI_API_KEY
    env_mode = config.ENV_MODE
"""

import os
from enum import Enum
from typing import Dict, Any, Optional, get_type_hints, Union
from dotenv import load_dotenv # type: ignore
import logging

logger = logging.getLogger(__name__)

class EnvMode(Enum):
    """Environment mode enumeration."""
    LOCAL = "local"
    STAGING = "staging"
    PRODUCTION = "production"

class Configuration:
    """
    Centralized configuration for AgentPress backend.
    
    This class loads environment variables and provides type checking and validation.
    Default values can be specified for optional configuration items.
    """
    
    # Environment mode
    ENV_MODE: EnvMode = EnvMode.LOCAL
    
    # Subscription tier IDs - Production
    STRIPE_FREE_TIER_ID_PROD: str = 'price_1RILb4G6l1KZGqIrK4QLrx9i'
    STRIPE_TIER_2_20_ID_PROD: str = 'price_1RILb4G6l1KZGqIrhomjgDnO'
    STRIPE_TIER_6_50_ID_PROD: str = 'price_1RILb4G6l1KZGqIr5q0sybWn'
    STRIPE_TIER_12_100_ID_PROD: str = 'price_1RILb4G6l1KZGqIr5Y20ZLHm'
    STRIPE_TIER_25_200_ID_PROD: str = 'price_1RILb4G6l1KZGqIrGAD8rNjb'
    STRIPE_TIER_50_400_ID_PROD: str = 'price_1RILb4G6l1KZGqIruNBUMTF1'
    STRIPE_TIER_125_800_ID_PROD: str = 'price_1RILb3G6l1KZGqIrbJA766tN'
    STRIPE_TIER_200_1000_ID_PROD: str = 'price_1RILb3G6l1KZGqIrmauYPOiN'
    
    # Yearly subscription tier IDs - Production (15% discount)
    STRIPE_TIER_2_20_YEARLY_ID_PROD: str = 'price_1ReHB5G6l1KZGqIrD70I1xqM'
    STRIPE_TIER_6_50_YEARLY_ID_PROD: str = 'price_1ReHAsG6l1KZGqIrlAog487C'
    STRIPE_TIER_12_100_YEARLY_ID_PROD: str = 'price_1ReHAWG6l1KZGqIrBHer2PQc'
    STRIPE_TIER_25_200_YEARLY_ID_PROD: str = 'price_1ReH9uG6l1KZGqIrsvMLHViC'
    STRIPE_TIER_50_400_YEARLY_ID_PROD: str = 'price_1ReH9fG6l1KZGqIrsPtu5KIA'
    STRIPE_TIER_125_800_YEARLY_ID_PROD: str = 'price_1ReH9GG6l1KZGqIrfgqaJyat'
    STRIPE_TIER_200_1000_YEARLY_ID_PROD: str = 'price_1ReH8qG6l1KZGqIrK1akY90q'

    # Yearly commitment prices - Production (15% discount, monthly payments with 12-month commitment via schedules)
    STRIPE_TIER_2_17_YEARLY_COMMITMENT_ID_PROD: str = 'price_1RqtqiG6l1KZGqIrhjVPtE1s'  # $17/month
    STRIPE_TIER_6_42_YEARLY_COMMITMENT_ID_PROD: str = 'price_1Rqtr8G6l1KZGqIrQ0ql0qHi'  # $42.50/month
    STRIPE_TIER_25_170_YEARLY_COMMITMENT_ID_PROD: str = 'price_1RqtrUG6l1KZGqIrEb8hLsk3'  # $170/month

    # Subscription tier IDs - Staging
    STRIPE_FREE_TIER_ID_STAGING: str = 'price_1RIGvuG6l1KZGqIrw14abxeL'
    STRIPE_TIER_2_20_ID_STAGING: str = 'price_1RIGvuG6l1KZGqIrCRu0E4Gi'
    STRIPE_TIER_6_50_ID_STAGING: str = 'price_1RIGvuG6l1KZGqIrvjlz5p5V'
    STRIPE_TIER_12_100_ID_STAGING: str = 'price_1RIGvuG6l1KZGqIrT6UfgblC'
    STRIPE_TIER_25_200_ID_STAGING: str = 'price_1RIGvuG6l1KZGqIrOVLKlOMj'
    STRIPE_TIER_50_400_ID_STAGING: str = 'price_1RIKNgG6l1KZGqIrvsat5PW7'
    STRIPE_TIER_125_800_ID_STAGING: str = 'price_1RIKNrG6l1KZGqIrjKT0yGvI'
    STRIPE_TIER_200_1000_ID_STAGING: str = 'price_1RIKQ2G6l1KZGqIrum9n8SI7'
    
    # Yearly subscription tier IDs - Staging (15% discount)
    STRIPE_TIER_2_20_YEARLY_ID_STAGING: str = 'price_1ReGogG6l1KZGqIrEyBTmtPk'
    STRIPE_TIER_6_50_YEARLY_ID_STAGING: str = 'price_1ReGoJG6l1KZGqIr0DJWtoOc'
    STRIPE_TIER_12_100_YEARLY_ID_STAGING: str = 'price_1ReGnZG6l1KZGqIr0ThLEl5S'
    STRIPE_TIER_25_200_YEARLY_ID_STAGING: str = 'price_1ReGmzG6l1KZGqIre31mqoEJ'
    STRIPE_TIER_50_400_YEARLY_ID_STAGING: str = 'price_1ReGmgG6l1KZGqIrn5nBc7e5'
    STRIPE_TIER_125_800_YEARLY_ID_STAGING: str = 'price_1ReGmMG6l1KZGqIrvE2ycrAX'
    STRIPE_TIER_200_1000_YEARLY_ID_STAGING: str = 'price_1ReGlXG6l1KZGqIrlgurP5GU'

    # Yearly commitment prices - Staging (15% discount, monthly payments with 12-month commitment via schedules)
    STRIPE_TIER_2_17_YEARLY_COMMITMENT_ID_STAGING: str = 'price_1RqYGaG6l1KZGqIrIzcdPzeQ'  # $17/month
    STRIPE_TIER_6_42_YEARLY_COMMITMENT_ID_STAGING: str = 'price_1RqYH1G6l1KZGqIrWDKh8xIU'  # $42.50/month
    STRIPE_TIER_25_170_YEARLY_COMMITMENT_ID_STAGING: str = 'price_1RqYHbG6l1KZGqIrAUVf8KpG'  # $170/month
    
    # Computed subscription tier IDs based on environment
    @property
    def STRIPE_FREE_TIER_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_FREE_TIER_ID_STAGING
        return self.STRIPE_FREE_TIER_ID_PROD
    
    @property
    def STRIPE_TIER_2_20_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_2_20_ID_STAGING
        return self.STRIPE_TIER_2_20_ID_PROD
    
    @property
    def STRIPE_TIER_6_50_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_6_50_ID_STAGING
        return self.STRIPE_TIER_6_50_ID_PROD
    
    @property
    def STRIPE_TIER_12_100_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_12_100_ID_STAGING
        return self.STRIPE_TIER_12_100_ID_PROD
    
    @property
    def STRIPE_TIER_25_200_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_25_200_ID_STAGING
        return self.STRIPE_TIER_25_200_ID_PROD
    
    @property
    def STRIPE_TIER_50_400_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_50_400_ID_STAGING
        return self.STRIPE_TIER_50_400_ID_PROD
    
    @property
    def STRIPE_TIER_125_800_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_125_800_ID_STAGING
        return self.STRIPE_TIER_125_800_ID_PROD
    
    @property
    def STRIPE_TIER_200_1000_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_200_1000_ID_STAGING
        return self.STRIPE_TIER_200_1000_ID_PROD
    
    # Yearly tier computed properties
    @property
    def STRIPE_TIER_2_20_YEARLY_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_2_20_YEARLY_ID_STAGING
        return self.STRIPE_TIER_2_20_YEARLY_ID_PROD
    
    @property
    def STRIPE_TIER_6_50_YEARLY_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_6_50_YEARLY_ID_STAGING
        return self.STRIPE_TIER_6_50_YEARLY_ID_PROD
    
    @property
    def STRIPE_TIER_12_100_YEARLY_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_12_100_YEARLY_ID_STAGING
        return self.STRIPE_TIER_12_100_YEARLY_ID_PROD
    
    @property
    def STRIPE_TIER_25_200_YEARLY_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_25_200_YEARLY_ID_STAGING
        return self.STRIPE_TIER_25_200_YEARLY_ID_PROD
    
    @property
    def STRIPE_TIER_50_400_YEARLY_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_50_400_YEARLY_ID_STAGING
        return self.STRIPE_TIER_50_400_YEARLY_ID_PROD
    
    @property
    def STRIPE_TIER_125_800_YEARLY_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_125_800_YEARLY_ID_STAGING
        return self.STRIPE_TIER_125_800_YEARLY_ID_PROD
    
    @property
    def STRIPE_TIER_200_1000_YEARLY_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_200_1000_YEARLY_ID_STAGING
        return self.STRIPE_TIER_200_1000_YEARLY_ID_PROD
    
    # Yearly commitment prices computed properties
    @property
    def STRIPE_TIER_2_17_YEARLY_COMMITMENT_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_2_17_YEARLY_COMMITMENT_ID_STAGING
        return self.STRIPE_TIER_2_17_YEARLY_COMMITMENT_ID_PROD

    @property
    def STRIPE_TIER_6_42_YEARLY_COMMITMENT_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_6_42_YEARLY_COMMITMENT_ID_STAGING
        return self.STRIPE_TIER_6_42_YEARLY_COMMITMENT_ID_PROD

    @property
    def STRIPE_TIER_25_170_YEARLY_COMMITMENT_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_TIER_25_170_YEARLY_COMMITMENT_ID_STAGING
        return self.STRIPE_TIER_25_170_YEARLY_COMMITMENT_ID_PROD
    
    # LLM API keys
    ANTHROPIC_API_KEY: Optional[str] = None
    OPENAI_API_KEY: Optional[str] = None
    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_API_BASE: Optional[str] = "https://api.deepseek.com"
    OLLAMA_API_KEY: Optional[str] = None
    GROQ_API_KEY: Optional[str] = None
    OPENROUTER_API_KEY: Optional[str] = None
    XAI_API_KEY: Optional[str] = None
    MORPH_API_KEY: Optional[str] = None
    GEMINI_API_KEY: Optional[str] = None
    GEMINI_API_BASE: Optional[str] = None
    DASHSCOPE_API_KEY: Optional[str] = None
    DASHSCOPE_COMPATIBLE_BASE_URL: Optional[str] = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    DASHSCOPE_API_BASE: Optional[str] = "https://dashscope.aliyuncs.com/api/v1"
    DASHSCOPE_BASE_URL: Optional[str] = None
    DASHSCOPE_VISION_MODEL: Optional[str] = None
    MOONSHOT_API_KEY: Optional[str] = None
    MOONSHOT_API_BASE: Optional[str] = "https://api.moonshot.cn/v1"
    MOONSHOT_BASE_URL: Optional[str] = None
    KIMI_MEDIA_MODEL: Optional[str] = "kimi-k2.6"
    KIMI_MEDIA_IMAGE_MAX_BYTES: int = 10 * 1024 * 1024
    KIMI_MEDIA_VIDEO_MAX_BYTES: int = 50 * 1024 * 1024
    KIMI_MEDIA_TIMEOUT_SECONDS: int = 60
    PPIO_API_KEY: Optional[str] = None
    PPIO_API_BASE: Optional[str] = "https://api.ppio.com/openai"
    PPIO_BASE_URL: Optional[str] = None
    VOLCENGINE_ARK_API_KEY: Optional[str] = None
    ARK_API_KEY: Optional[str] = None
    VOLCENGINE_ARK_BASE_URL: Optional[str] = "https://ark.cn-beijing.volces.com/api/v3"
    ARK_BASE_URL: Optional[str] = None
    OPENAI_PROXY_API_KEY: Optional[str] = None
    OPENAI_PROXY_BASE_URL: Optional[str] = "https://api.openai-proxy.org/v1"
    OPENAI_PROXY_GPT_5_4_MODEL_NAME: Optional[str] = "gpt-5.4"
    OPENROUTER_API_BASE: Optional[str] = "https://openrouter.ai/api/v1"
    OPENROUTER_BASE_URL: Optional[str] = None
    OR_SITE_URL: Optional[str] = "https://fufanmanus.com"
    OR_APP_NAME: Optional[str] = "Fufanmanus"
    
    # AWS Bedrock credentials
    AWS_ACCESS_KEY_ID: Optional[str] = None
    AWS_SECRET_ACCESS_KEY: Optional[str] = None
    AWS_REGION_NAME: Optional[str] = None
    
    # Model configuration
    MODEL_TO_USE: Optional[str] = "gemini/gemini-3.1-pro-preview"

    # Agent backend selection
    AGENT_BACKEND: Optional[str] = "agentscope"  # "agentscope" | "claude_sdk"
    CLAUDE_SANDBOX_TEMPLATE: Optional[str] = "claude-agent-v1"  # PPIO template name
    CLAUDE_SANDBOX_TEMPLATE_ID: Optional[str] = ""  # PPIO-assigned template ID
    
    # Supabase configuration (optional for simple auth testing)
    SUPABASE_URL: Optional[str] = None
    SUPABASE_ANON_KEY: Optional[str] = None
    SUPABASE_SERVICE_ROLE_KEY: Optional[str] = None
    
    # JWT Authentication configuration
    JWT_SECRET_KEY: Optional[str] = None
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60  # 1小时
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30    # 30天
    
    # Redis configuration (optional for simple auth testing)
    REDIS_HOST: Optional[str] = "localhost"
    REDIS_PORT: int = 6379
    REDIS_PASSWORD: Optional[str] = None
    REDIS_SSL: bool = False
    
    # PPIP/E2B 沙箱配置
    E2B_API_KEY: Optional[str] = None
    E2B_DOMAIN: str = "sandbox.ppio.cn"  # PPIO 沙箱域名
    SANDBOX_IO_TIMEOUT_SECONDS: float = 20.0
    SANDBOX_CONNECT_TIMEOUT_SECONDS: float = 20.0
    SANDBOX_CONNECT_RETRY_COUNT: int = 0
    SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS: float = 0.4
    SANDBOX_RECOVERY_MAX_ATTEMPTS: int = 1
    SANDBOX_IO_QUEUE_TIMEOUT_SECONDS: float = 3.0
    SANDBOX_IO_MAX_CONCURRENT_GLOBAL: int = 24
    SANDBOX_IO_MAX_CONCURRENT_PER_SANDBOX: int = 10
    SANDBOX_ARCHIVE_DOWNLOAD_ENABLED: bool = True
    SANDBOX_AUTO_PAUSE_ON_COMPLETE: bool = True
    SANDBOX_AUTO_PAUSE_DELAY_ENABLED: bool = True
    SANDBOX_AUTO_PAUSE_GRACE_SECONDS: int = 300
    SANDBOX_REATTACH_BUDGET_SECONDS: float = 5.0
    SANDBOX_REATTACH_PROBE_TIMEOUT_SECONDS: float = 1.5
    SANDBOX_IDLE_TIMEOUT_SECONDS: int = 3600
    SANDBOX_CONNECT_TIMEOUT_ON_RESUME_SECONDS: int = 3600
    SANDBOX_PRECLONE_ENABLED: bool = True
    SANDBOX_PRECLONE_THRESHOLD_SECONDS: int = 3000
    SANDBOX_PRECLONE_RETRY_SECONDS: int = 60
    SANDBOX_PRECLONE_GRACE_CLEANUP_SECONDS: int = 600
    SANDBOX_PRECLONE_CLONE_TIMEOUT_SECONDS: int = 3600
    SANDBOX_CLONE_RENEWAL_DAYS: int = 29
    WORKSPACE_ARTIFACTS_ENABLED: bool = True
    WORKSPACE_ARTIFACTS_BACKEND: str = "local"
    WORKSPACE_ARTIFACTS_LOCAL_ROOT: Optional[str] = None
    WORKSPACE_ARTIFACTS_REHYDRATE_ON_RECOVERY: bool = True
    WORKSPACE_ARTIFACTS_OSS_ENDPOINT: Optional[str] = None
    WORKSPACE_ARTIFACTS_OSS_BUCKET: Optional[str] = None
    WORKSPACE_ARTIFACTS_OSS_ACCESS_KEY_ID: Optional[str] = None
    WORKSPACE_ARTIFACTS_OSS_ACCESS_KEY_SECRET: Optional[str] = None
    WORKSPACE_ARTIFACTS_OSS_PREFIX: Optional[str] = "workspace-artifacts"

    # Netlify 部署配置
    NETLIFY_AUTH_TOKEN: Optional[str] = None
    NETLIFY_TEAM_SLUG: Optional[str] = None
    
    
    SANDBOX_TEMPLATE_CODE: Optional[str] = os.getenv('SANDBOX_TEMPLATE_CODE', '5iwky193kz9r0woswit9')  # agent-skills-v11 code
    SANDBOX_TEMPLATE_DESKTOP: Optional[str] = os.getenv('SANDBOX_TEMPLATE_DESKTOP', '4imxoe43snzcxj95hvha') # desktop (VNC)
    SANDBOX_TEMPLATE_BROWSER: Optional[str] = os.getenv('SANDBOX_TEMPLATE_BROWSER', '5iwky193kz9r0woswit9') # agent-skills-v11 browser
    SANDBOX_TEMPLATE_BASE: Optional[str] = os.getenv('SANDBOX_TEMPLATE_BASE', 'yem8b4s8tqlidov2zme8') # base
    SANDBOX_TEMPLATE_CLAUDE: Optional[str] = os.getenv('SANDBOX_TEMPLATE_CLAUDE', '')  # claude-agent-v1 (PPIO-assigned)
    SHADOW_CLONE_SANDBOX_TYPE: str = os.getenv('SHADOW_CLONE_SANDBOX_TYPE', 'code')

    # Skill externalization mode — controls whether skills are loaded from
    # the local filesystem (fast iteration) or the sandbox template (production).
    # true  = skills uploaded from backend/claude_skills/ at runtime (no template rebuild needed)
    # false = skills copied from /opt/claude_skills/ inside the template (existing behavior)
    SKILL_EXPERIMENT_MODE: bool = os.getenv('SKILL_EXPERIMENT_MODE', 'true').lower() == 'true'
    
    # 默认模板类型 - 用桌面模板来支持 VNC 和浏览器功能
    DEFAULT_SANDBOX_TYPE: str = "desktop"
    
    def get_sandbox_template(self, sandbox_type: Optional[str] = None) -> str:
        """获取指定类型的沙箱模板 ID"""
        template_type = sandbox_type or os.getenv('SANDBOX_TYPE', self.DEFAULT_SANDBOX_TYPE)
        
        # 根据类型从环境变量获取模板ID
        template_mapping = {
            'code': self.SANDBOX_TEMPLATE_CODE,
            'desktop': self.SANDBOX_TEMPLATE_DESKTOP,
            'browser': self.SANDBOX_TEMPLATE_BROWSER,
            'base': self.SANDBOX_TEMPLATE_BASE,
            'claude_sdk': self.SANDBOX_TEMPLATE_CLAUDE,
        }
        
        template_id = template_mapping.get(template_type)
        if not template_id:
            logger.warning(f"No template found for type '{template_type}', falling back to desktop")
            template_id = self.SANDBOX_TEMPLATE_DESKTOP
        
        # 验证模板ID格式
        if not isinstance(template_id, str) or not template_id.strip():
            logger.error(f"Invalid template ID for type '{template_type}': {template_id}")
            raise ValueError(f"Invalid template ID for sandbox type '{template_type}'. Please check your .env configuration.")
            
        logger.debug(f"Using template {template_id} for sandbox type '{template_type}'")
        return template_id

    def get_shadow_clone_sandbox_type(self) -> str:
        """Resolve the shared sandbox type used by strict Shadow Clone runs."""
        sandbox_type = str(self.SHADOW_CLONE_SANDBOX_TYPE or "code").strip().lower() or "code"
        if sandbox_type not in {"code", "desktop", "browser", "base"}:
            logger.warning(
                "Invalid SHADOW_CLONE_SANDBOX_TYPE '%s', falling back to 'code'",
                sandbox_type,
            )
            return "code"
        return sandbox_type
    
    # Daytona sandbox configuration (deprecated - 保留以供回退)
    DAYTONA_API_KEY: Optional[str] = None
    DAYTONA_SERVER_URL: Optional[str] = None
    DAYTONA_TARGET: Optional[str] = None
    
    # Search and other API keys (optional for simple auth testing)
    TAVILY_API_KEY: Optional[str] = None
    RAPID_API_KEY: Optional[str] = None
    CLOUDFLARE_API_TOKEN: Optional[str] = None
    FIRECRAWL_API_KEY: Optional[str] = None
    FIRECRAWL_URL: Optional[str] = "https://api.firecrawl.dev"
    
    # Stripe configuration
    STRIPE_SECRET_KEY: Optional[str] = None
    STRIPE_WEBHOOK_SECRET: Optional[str] = None
    STRIPE_DEFAULT_PLAN_ID: Optional[str] = None
    STRIPE_DEFAULT_TRIAL_DAYS: int = 14
    
    # Stripe Product IDs
    STRIPE_PRODUCT_ID_PROD: str = 'prod_SCl7AQ2C8kK1CD'
    STRIPE_PRODUCT_ID_STAGING: str = 'prod_SCgIj3G7yPOAWY'
    
    # Sandbox configuration
    SANDBOX_IMAGE_NAME = "fufan/manus:0.1"
    
    # 🔧 PPIP/E2B 沙箱配置已在上面定义，此处移除重复
    
    # 保留原配置以供回退
    SANDBOX_SNAPSHOT_NAME: str = "fufan/manus:0.1"  # Deprecated - Daytona 快照名
    SANDBOX_ENTRYPOINT = "/usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf"

    # LangFuse configuration
    LANGFUSE_PUBLIC_KEY: Optional[str] = None
    LANGFUSE_SECRET_KEY: Optional[str] = None
    LANGFUSE_HOST: str = "https://cloud.langfuse.com"

    # Admin API key for server-side operations
    KORTIX_ADMIN_API_KEY: Optional[str] = None

    # API Keys system configuration
    API_KEY_SECRET: str = "default-secret-key-change-in-production"
    API_KEY_LAST_USED_THROTTLE_SECONDS: int = 900
    
    # Agent execution limits (can be overridden via environment variable)
    _MAX_PARALLEL_AGENT_RUNS_ENV: Optional[str] = None
    
    # Agent limits per billing tier
    # Note: These limits are bypassed in local mode (ENV_MODE=local) where unlimited agents are allowed
    AGENT_LIMITS = {
        'free': 2,
        'tier_2_20': 5,
        'tier_6_50': 20,
        'tier_12_100': 20,
        'tier_25_200': 100,
        'tier_50_400': 100,
        'tier_125_800': 100,
        'tier_200_1000': 100,
        # Yearly plans have same limits as monthly
        'tier_2_20_yearly': 5,
        'tier_6_50_yearly': 20,
        'tier_12_100_yearly': 20,
        'tier_25_200_yearly': 100,
        'tier_50_400_yearly': 100,
        'tier_125_800_yearly': 100,
        'tier_200_1000_yearly': 100,
        # Yearly commitment plans
        'tier_2_17_yearly_commitment': 5,
        'tier_6_42_yearly_commitment': 20,
        'tier_25_170_yearly_commitment': 100,
    }

    @property
    def MAX_PARALLEL_AGENT_RUNS(self) -> int:
        """
        Get the maximum parallel agent runs limit.
        
        Can be overridden via MAX_PARALLEL_AGENT_RUNS environment variable.
        Defaults:
        - Production: 3
        - Local/Staging: 999999 (effectively infinite)
        """
        # Check for environment variable override first
        if self._MAX_PARALLEL_AGENT_RUNS_ENV is not None:
            try:
                return int(self._MAX_PARALLEL_AGENT_RUNS_ENV)
            except ValueError:
                logger.warning(f"Invalid MAX_PARALLEL_AGENT_RUNS value: {self._MAX_PARALLEL_AGENT_RUNS_ENV}, using default")
        
        # Environment-based defaults
        if self.ENV_MODE == EnvMode.PRODUCTION:
            return 3
        else:
            # Local and staging: effectively infinite
            return 999999
    
    @property
    def STRIPE_PRODUCT_ID(self) -> str:
        if self.ENV_MODE == EnvMode.STAGING:
            return self.STRIPE_PRODUCT_ID_STAGING
        return self.STRIPE_PRODUCT_ID_PROD
    
    def __init__(self):
        """Initialize configuration by loading from environment variables."""
        # Load environment variables from .env file if it exists
        load_dotenv(override=False)
        
        # Set environment mode first
        env_mode_str = os.getenv("ENV_MODE", EnvMode.LOCAL.value)
        try:
            self.ENV_MODE = EnvMode(env_mode_str.lower())
        except ValueError:
            logger.warning(f"Invalid ENV_MODE: {env_mode_str}, defaulting to LOCAL")
            self.ENV_MODE = EnvMode.LOCAL
            
        logger.info(f"Environment mode: {self.ENV_MODE.value}")
        
        # Load configuration from environment variables
        self._load_from_env()

        openrouter_base_url = os.getenv("OPENROUTER_BASE_URL")
        if openrouter_base_url:
            self.OPENROUTER_BASE_URL = openrouter_base_url
            if not os.getenv("OPENROUTER_API_BASE"):
                self.OPENROUTER_API_BASE = openrouter_base_url

        moonshot_base_url = os.getenv("MOONSHOT_BASE_URL")
        if moonshot_base_url:
            self.MOONSHOT_BASE_URL = moonshot_base_url
            if not os.getenv("MOONSHOT_API_BASE"):
                self.MOONSHOT_API_BASE = moonshot_base_url

        ppio_base_url = os.getenv("PPIO_BASE_URL")
        if ppio_base_url:
            self.PPIO_BASE_URL = ppio_base_url
            if not os.getenv("PPIO_API_BASE"):
                self.PPIO_API_BASE = ppio_base_url

        openai_proxy_base_url = os.getenv("OPENAI_PROXY_BASE_URL")
        if openai_proxy_base_url:
            self.OPENAI_PROXY_BASE_URL = openai_proxy_base_url

        # Perform validation
        self._validate()
        
    def _load_from_env(self):
        """Load configuration values from environment variables."""
        for key, expected_type in get_type_hints(self.__class__).items():
            env_val = os.getenv(key)
            
            if env_val is not None:
                # Convert environment variable to the expected type
                if expected_type == bool:
                    # Handle boolean conversion
                    setattr(self, key, env_val.lower() in ('true', 't', 'yes', 'y', '1'))
                elif expected_type == int:
                    # Handle integer conversion
                    try:
                        setattr(self, key, int(env_val))
                    except ValueError:
                        logger.warning(f"Invalid value for {key}: {env_val}, using default")
                elif expected_type == EnvMode:
                    # Already handled for ENV_MODE
                    pass
                else:
                    # String or other type
                    setattr(self, key, env_val)
        
        # Custom handling for environment-dependent properties
        max_parallel_runs_env = os.getenv("MAX_PARALLEL_AGENT_RUNS")
        if max_parallel_runs_env is not None:
            self._MAX_PARALLEL_AGENT_RUNS_ENV = max_parallel_runs_env
    
    def _validate(self):
        """Validate configuration based on type hints."""
        # Get all configuration fields and their type hints
        type_hints = get_type_hints(self.__class__)
        
        # Find missing required fields
        missing_fields = []
        for field, field_type in type_hints.items():
            # Check if the field is Optional
            is_optional = hasattr(field_type, "__origin__") and field_type.__origin__ is Union and type(None) in field_type.__args__
            
            # If not optional and value is None, add to missing fields
            if not is_optional and getattr(self, field) is None:
                missing_fields.append(field)
        
        if missing_fields:
            error_msg = f"Missing required configuration fields: {', '.join(missing_fields)}"
            logger.error(error_msg)
            raise ValueError(error_msg)
    
    def get(self, key: str, default: Any = None) -> Any:
        """Get a configuration value with an optional default."""
        return getattr(self, key, default)
    
    def as_dict(self) -> Dict[str, Any]:
        """Return configuration as a dictionary."""
        return {
            key: getattr(self, key) 
            for key in get_type_hints(self.__class__).keys()
            if not key.startswith('_')
        }

# Create a singleton instance
config = Configuration() 
