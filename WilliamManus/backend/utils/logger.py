import structlog, logging, os # type: ignore
import re
from urllib.parse import parse_qsl, urlencode
from logging.handlers import RotatingFileHandler
import datetime

ENV_MODE = os.getenv("ENV_MODE", "LOCAL")
REQUEST_ID_HEADER = "X-Request-ID"
CLIENT_OPERATION_ID_HEADER = "X-Client-Operation-ID"
REDACTED_VALUE = "REDACTED"

_CORRELATION_ID_PATTERN = re.compile(r"[^A-Za-z0-9._:-]+")
_SENSITIVE_QUERY_KEYS = {
    "accesstoken",
    "authorization",
    "authtoken",
    "code",
    "cookie",
    "idtoken",
    "jwt",
    "refreshtoken",
    "sessionid",
    "signature",
    "sig",
    "token",
}


def sanitize_correlation_id(value: str | None, *, max_length: int = 128) -> str | None:
    if value is None:
        return None

    sanitized = _CORRELATION_ID_PATTERN.sub("", value.strip())
    if not sanitized:
        return None

    return sanitized[:max_length]


def _is_sensitive_query_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    if not normalized:
        return False

    return normalized in _SENSITIVE_QUERY_KEYS or normalized.endswith("token")


def sanitize_query_params(raw_query: str | None) -> str:
    if not raw_query:
        return ""

    try:
        query_pairs = parse_qsl(raw_query, keep_blank_values=True)
    except Exception:
        return REDACTED_VALUE

    sanitized_pairs = []
    for key, value in query_pairs:
        sanitized_value = REDACTED_VALUE if _is_sensitive_query_key(key) else value
        sanitized_pairs.append((key, sanitized_value))

    return urlencode(sanitized_pairs, doseq=True)

# 创建logs目录
logs_dir = "logs"
if not os.path.exists(logs_dir):
    os.makedirs(logs_dir)

# 设置日志文件路径，包含日期
log_filename = f"logs/app_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

# 根据环境设置默认日志级别
if ENV_MODE.upper() == "PRODUCTION":
    default_level = "WARNING"  # 生产环境只显示警告和错误
else:
    default_level = "INFO"  # 开发环境显示INFO级别及以上


# 直接设置为 INFO 级别来测试
LOGGING_LEVEL = logging.INFO

# 原来的逻辑（先注释掉测试）
# LOGGING_LEVEL = logging.getLevelNamesMapping().get(
#     os.getenv("LOGGING_LEVEL", default_level).upper(), 
#     logging.INFO  # 默认使用INFO级别
# )

# 使用简单的Python标准库logging配置，避免多进程兼容性问题
# 清除现有的handlers
root_logger = logging.getLogger()
for handler in root_logger.handlers[:]:
    root_logger.removeHandler(handler)

# 配置控制台handler
console_handler = logging.StreamHandler()
console_formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
)
console_handler.setFormatter(console_formatter)

# 配置文件handler
file_handler = RotatingFileHandler(
    log_filename,
    maxBytes=10*1024*1024,  # 10MB
    backupCount=5,
    encoding='utf-8'
)
file_formatter = logging.Formatter(
    '%(asctime)s [%(levelname)s] [%(name)s] %(message)s'
)
file_handler.setFormatter(file_formatter)

# 配置根logger
root_logger.setLevel(LOGGING_LEVEL)
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)

# 简化structlog配置，避免多进程兼容性问题
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
    ],
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
    wrapper_class=structlog.stdlib.BoundLogger,
)

logger: structlog.stdlib.BoundLogger = structlog.get_logger()

# # Debug: Print actual configuration
# print(f"DEBUG Logger Config: ENV_MODE={ENV_MODE}, LOGGING_LEVEL={LOGGING_LEVEL}")
# print(f"DEBUG Logger Config: LOGGING_LEVEL numeric={LOGGING_LEVEL}")
# print(f"DEBUG Logger Config: INFO level={logging.INFO}")
# print(f"DEBUG Logger Config: Will INFO show? {LOGGING_LEVEL <= logging.INFO}")

# # Test logger immediately after configuration
# print("DEBUG: Testing logger immediately...")
# logger.info("DEBUG: This is a test INFO message from logger initialization")
# logger.warning("DEBUG: This is a test WARNING message from logger initialization")
# print("DEBUG: Logger test completed")
