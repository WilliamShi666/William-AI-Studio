from dotenv import load_dotenv
from sandbox.session_control import get_shadow_clone_session_state
from services import sandbox_create_capacity
from utils.logger import logger
from utils.config import config
from utils.config import Configuration
import os
import json
import asyncio
import inspect
import math
import re
import time
from typing import Any, Optional
# PPIO 沙箱 SDK - 根据类型动态导入

load_dotenv(override=False)

logger.debug("Initializing PPIP/E2B sandbox configuration")

# PPIO/E2B 配置 - 根据 PPIO 文档要求:https://ppio.com/docs/sandbox/get-start
# 设置 PPIO 沙箱域名
os.environ['E2B_DOMAIN'] = getattr(config, 'E2B_DOMAIN', 'sandbox.ppio.cn')

# 设置 E2B API Key
if hasattr(config, 'E2B_API_KEY') and config.E2B_API_KEY:
    os.environ['E2B_API_KEY'] = config.E2B_API_KEY
    logger.debug("E2B API key configured successfully")
elif 'E2B_API_KEY' in os.environ:
    logger.debug("E2B API key found in environment variables")
else:
    logger.warning("No E2B API key found in environment variables")

logger.debug(f"PPIO E2B Domain set to: {os.environ.get('E2B_DOMAIN')}")
logger.debug(f"E2B API Key configured: {'Yes' if os.environ.get('E2B_API_KEY') else 'No'}")


def _safe_positive_float(raw_value, default_value: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(raw_value))
    except (TypeError, ValueError):
        return default_value


def _get_reattach_budget_seconds() -> float:
    return _safe_positive_float(
        getattr(config, 'SANDBOX_REATTACH_BUDGET_SECONDS', 5.0),
        5.0,
    )


def _get_reattach_probe_timeout_seconds() -> float:
    return _safe_positive_float(
        getattr(config, 'SANDBOX_REATTACH_PROBE_TIMEOUT_SECONDS', 1.5),
        1.5,
        minimum=0.1,
    )


def _get_resume_nudge_timeout_seconds() -> float:
    return min(
        _get_reattach_budget_seconds(),
        _get_reattach_probe_timeout_seconds(),
    )


def _get_resume_timeout_seconds() -> int:
    raw_timeout = getattr(config, 'SANDBOX_CONNECT_TIMEOUT_ON_RESUME_SECONDS', 3600)
    try:
        return max(60, int(raw_timeout))
    except (TypeError, ValueError):
        return 3600


def _get_delete_timeout_seconds() -> float:
    return _safe_positive_float(
        getattr(config, "SANDBOX_DELETE_TIMEOUT_SECONDS", 15.0),
        15.0,
        minimum=1.0,
    )


def _get_create_retry_max_attempts() -> int:
    raw_value = os.getenv("SANDBOX_CREATE_RETRY_MAX_ATTEMPTS", "1")
    try:
        return max(1, int(raw_value))
    except (TypeError, ValueError):
        return 1


def _get_create_retry_base_backoff_seconds() -> float:
    return _safe_positive_float(
        os.getenv("SANDBOX_CREATE_RETRY_BASE_BACKOFF_SECONDS", "0.5"),
        0.5,
        minimum=0.0,
    )


def _get_create_retry_max_backoff_seconds() -> float:
    return _safe_positive_float(
        os.getenv("SANDBOX_CREATE_RETRY_MAX_BACKOFF_SECONDS", "8.0"),
        8.0,
        minimum=0.1,
    )


def _get_timeout_lease_window_seconds() -> int:
    raw_value = os.getenv("SANDBOX_SET_TIMEOUT_WINDOW_SECONDS", "1800")
    try:
        return max(60, int(raw_value))
    except (TypeError, ValueError):
        return 1800


def _get_timeout_lease_connect_timeout_seconds() -> float:
    return _safe_positive_float(
        os.getenv("SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS", "20"),
        20.0,
        minimum=1.0,
    )


def _is_sandbox_not_found_error(error: Exception) -> bool:
    message = str(error).lower()
    patterns = (
        "not found",
        "sandboxnotfound",
        "does not exist",
        "no such sandbox",
        "404",
    )
    return any(pattern in message for pattern in patterns)


def resolve_sandbox_template_lineage(sandbox_type: str | None = None) -> dict[str, str]:
    resolved_type = str(
        sandbox_type
        or os.getenv('SANDBOX_TYPE')
        or getattr(config, 'DEFAULT_SANDBOX_TYPE', 'desktop')
    ).strip() or 'desktop'
    template_id = str(config.get_sandbox_template(resolved_type) or '').strip()
    return {
        "template_id": template_id,
        "template_type": resolved_type,
        "template_source": "config_env",
    }


def extract_sandbox_template_id(sandbox_obj: Any) -> str | None:
    direct_candidate = str(
        getattr(sandbox_obj, "template_id", None)
        or getattr(sandbox_obj, "template", None)
        or ""
    ).strip()
    if direct_candidate:
        return direct_candidate

    build_info = getattr(sandbox_obj, "build_info", None)
    if build_info is not None:
        build_candidate = str(
            getattr(build_info, "template_id", None)
            or getattr(build_info, "template", None)
            or ""
        ).strip()
        if build_candidate:
            return build_candidate

    metadata = getattr(sandbox_obj, "metadata", None)
    if isinstance(metadata, dict):
        metadata_candidate = str(
            metadata.get("template_id")
            or metadata.get("template")
            or ""
        ).strip()
        if metadata_candidate:
            return metadata_candidate

    return None


class SandboxOriginalReuseExhausted(RuntimeError):
    """Raised when original sandbox recovery was exhausted and caller must choose fallback behavior."""

    def __init__(
        self,
        sandbox_id: str,
        sandbox_type: str,
        project_id: str,
        *,
        last_error: Exception | None = None,
    ) -> None:
        self.sandbox_id = sandbox_id
        self.sandbox_type = sandbox_type
        self.project_id = project_id
        self.last_error = last_error
        detail = str(last_error) if last_error else "unknown error"
        super().__init__(
            f"Original sandbox {sandbox_id or '<missing>'} ({sandbox_type}) recovery exhausted "
            f"for project {project_id}: {detail}"
        )


_PROVIDER_ERROR_PATTERN = re.compile(
    r"(?:(?P<http_status>\d+)\s*:\s*)?error:\s*code\s*=\s*(?P<provider_code>\d+)\s*"
    r"reason\s*=\s*(?P<reason>[A-Z0-9_]+)\s*message\s*=\s*(?P<message>.*?)(?:\s+metadata\s*=|\s+cause\s*=|$)",
    re.IGNORECASE,
)
_PROVIDER_HTTP_STATUS_PREFIX_PATTERN = re.compile(
    r"^\s*(?:http\s+)?(?P<http_status>\d{3})(?:(?:\s*[:-]\s*|\s+)(?P<message>.*)|\s*$)",
    re.IGNORECASE,
)
_PROVIDER_RETRYABLE_HTTP_STATUSES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
_PROVIDER_RETRYABLE_REASONS = frozenset(
    {
        "RATE_LIMITED",
        "TOO_MANY_REQUESTS",
        "TIMEOUT",
        "UNAVAILABLE",
        "SERVICE_UNAVAILABLE",
        "INTERNAL_ERROR",
        "INTERNAL",
    }
)
_PROVIDER_REASON_ROOT_CAUSE_CODES = {
    "BALANCE_NOT_ENOUGH": ("SANDBOX_PROVIDER_BALANCE_NOT_ENOUGH", False),
    "UNAUTHORIZED": ("SANDBOX_PROVIDER_UNAUTHORIZED", False),
    "AUTHENTICATION_FAILED": ("SANDBOX_PROVIDER_UNAUTHORIZED", False),
    "FORBIDDEN": ("SANDBOX_PROVIDER_FORBIDDEN", False),
    "INVALID_API_KEY": ("SANDBOX_PROVIDER_UNAUTHORIZED", False),
    "RATE_LIMITED": ("SANDBOX_PROVIDER_RATE_LIMITED", True),
    "TOO_MANY_REQUESTS": ("SANDBOX_PROVIDER_RATE_LIMITED", True),
    "TIMEOUT": ("SANDBOX_PROVIDER_TIMEOUT", True),
    "UNAVAILABLE": ("SANDBOX_PROVIDER_UNAVAILABLE", True),
    "SERVICE_UNAVAILABLE": ("SANDBOX_PROVIDER_UNAVAILABLE", True),
}
_PROVIDER_HTTP_STATUS_REASON_HINTS = {
    408: "TIMEOUT",
    429: "RATE_LIMITED",
    500: "INTERNAL_ERROR",
    502: "UNAVAILABLE",
    503: "SERVICE_UNAVAILABLE",
    504: "TIMEOUT",
}
_PROVIDER_MESSAGE_REASON_HINTS = (
    ("balance not enough", "BALANCE_NOT_ENOUGH"),
    ("rate limit exceeded", "RATE_LIMITED"),
    ("too many requests", "TOO_MANY_REQUESTS"),
    ("timed out", "TIMEOUT"),
    ("timeout", "TIMEOUT"),
    ("service unavailable", "SERVICE_UNAVAILABLE"),
    ("temporarily unavailable", "SERVICE_UNAVAILABLE"),
    ("internal server error", "INTERNAL_ERROR"),
)
_PROVIDER_EXCEPTION_REASON_HINTS = (
    ("ratelimit", "RATE_LIMITED"),
    ("too_many_requests", "TOO_MANY_REQUESTS"),
    ("timeout", "TIMEOUT"),
    ("unavailable", "SERVICE_UNAVAILABLE"),
)


class SandboxProviderFailure(RuntimeError):
    """Typed provider-side failure surfaced from the sandbox SDK layer."""

    def __init__(
        self,
        *,
        operation: str,
        detail: str,
        root_cause_code: str,
        retryable: bool,
        sandbox_id: str = "",
        sandbox_type: str = "",
        provider_reason: Optional[str] = None,
        provider_http_status: Optional[int] = None,
        provider_code: Optional[int] = None,
    ) -> None:
        self.operation = str(operation or "").strip() or "provider"
        self.detail = str(detail or "").strip() or "Sandbox provider error"
        self.root_cause_code = str(root_cause_code or "").strip() or "SANDBOX_PROVIDER_ERROR"
        self.retryable = bool(retryable)
        self.sandbox_id = str(sandbox_id or "").strip()
        self.sandbox_type = str(sandbox_type or "").strip()
        self.provider_reason = str(provider_reason or "").strip() or None
        self.provider_http_status = provider_http_status
        self.provider_code = provider_code
        super().__init__(self.detail)


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_provider_http_status(message: str) -> tuple[Optional[int], str]:
    match = _PROVIDER_HTTP_STATUS_PREFIX_PATTERN.match(message)
    if match is None:
        return None, message

    provider_http_status = _safe_int(match.group("http_status"))
    trimmed_message = str(match.group("message") or "").strip()
    return provider_http_status, trimmed_message or message


def _infer_provider_reason(
    *,
    error: BaseException,
    message: str,
    provider_http_status: Optional[int],
) -> Optional[str]:
    lowered_message = message.lower()
    for hint, reason in _PROVIDER_MESSAGE_REASON_HINTS:
        if hint in lowered_message:
            return reason

    if provider_http_status in _PROVIDER_HTTP_STATUS_REASON_HINTS:
        return _PROVIDER_HTTP_STATUS_REASON_HINTS[provider_http_status]

    error_type_haystack = f"{type(error).__module__}.{type(error).__qualname__}".lower()
    for hint, reason in _PROVIDER_EXCEPTION_REASON_HINTS:
        if hint in error_type_haystack:
            return reason

    return None


def classify_sandbox_provider_failure(
    error: BaseException,
    *,
    operation: str,
    sandbox_id: str = "",
    sandbox_type: str = "",
) -> Optional[SandboxProviderFailure]:
    if isinstance(error, SandboxProviderFailure):
        return error

    message = str(error or "").strip()
    if not message:
        return None

    match = _PROVIDER_ERROR_PATTERN.search(message)
    provider_reason = None
    provider_http_status = None
    provider_code = None
    provider_message = message

    if match is not None:
        provider_reason = str(match.group("reason") or "").strip().upper() or None
        provider_http_status = _safe_int(match.group("http_status"))
        provider_code = _safe_int(match.group("provider_code"))
        parsed_message = str(match.group("message") or "").strip()
        if parsed_message:
            provider_message = parsed_message
    else:
        provider_http_status, provider_message = _extract_provider_http_status(message)

    if not provider_reason:
        provider_reason = _infer_provider_reason(
            error=error,
            message=provider_message,
            provider_http_status=provider_http_status,
        )

    if not provider_reason and provider_http_status is None:
        return None

    default_retryable = (
        provider_reason in _PROVIDER_RETRYABLE_REASONS
        or provider_http_status in _PROVIDER_RETRYABLE_HTTP_STATUSES
    )
    root_cause_code, retryable = _PROVIDER_REASON_ROOT_CAUSE_CODES.get(
        provider_reason,
        (
            "SANDBOX_PROVIDER_RETRYABLE_ERROR" if default_retryable else "SANDBOX_PROVIDER_ERROR",
            default_retryable,
        ),
    )
    reason_label = provider_reason or (
        f"HTTP_{provider_http_status}" if provider_http_status is not None else "UNKNOWN"
    )
    detail = (
        f"Sandbox provider {operation} failed for {sandbox_id or '<unknown>'}"
        f" ({sandbox_type or 'unknown'}): {reason_label}"
    )
    if provider_message:
        detail = f"{detail}: {provider_message}"

    return SandboxProviderFailure(
        operation=operation,
        detail=detail,
        root_cause_code=root_cause_code,
        retryable=retryable,
        sandbox_id=sandbox_id,
        sandbox_type=sandbox_type,
        provider_reason=provider_reason,
        provider_http_status=provider_http_status,
        provider_code=provider_code,
    )


def _coerce_sandbox_provider_failure(
    error: BaseException,
    *,
    operation: str,
    sandbox_id: str = "",
    sandbox_type: str = "",
) -> BaseException:
    classified = classify_sandbox_provider_failure(
        error,
        operation=operation,
        sandbox_id=sandbox_id,
        sandbox_type=sandbox_type,
    )
    return classified or error


async def _probe_sandbox_running(sandbox_obj, *, sandbox_id: str, timeout_seconds: float) -> bool:
    is_running_callable = getattr(sandbox_obj, "is_running", None)
    if not callable(is_running_callable):
        return True

    try:
        probe_result = await asyncio.wait_for(
            asyncio.to_thread(is_running_callable),
            timeout=timeout_seconds,
        )
        if inspect.isawaitable(probe_result):
            probe_result = await asyncio.wait_for(probe_result, timeout=timeout_seconds)
        return bool(probe_result)
    except Exception as probe_error:
        logger.warning(
            f"Sandbox liveliness probe failed for {sandbox_id}: {probe_error}"
        )
        return False


async def _attempt_ppio_resume(
    sandbox_id: str,
    *,
    timeout_seconds: float | None = None,
) -> tuple[bool, Optional[BaseException]]:
    """Best-effort explicit resume for paused sandboxes before SDK reattach."""
    try:
        from ppio_sandbox.core import Sandbox as PPIOSandbox

        provider_timeout = (
            max(1, math.ceil(timeout_seconds))
            if timeout_seconds is not None
            else _get_resume_timeout_seconds()
        )
        resume_call = asyncio.to_thread(
            PPIOSandbox._cls_resume,
            sandbox_id,
            timeout=provider_timeout,
        )
        resumed = (
            await asyncio.wait_for(resume_call, timeout=timeout_seconds)
            if timeout_seconds is not None
            else await resume_call
        )
        if resumed:
            logger.info(f"Requested explicit sandbox resume for {sandbox_id}")
            return True, None
    except asyncio.TimeoutError:
        logger.warning(
            "Explicit sandbox resume nudge timed out for %s after %.2fs",
            sandbox_id,
            timeout_seconds or 0.0,
        )
        return False, None
    except Exception as resume_error:
        normalized_error = _coerce_sandbox_provider_failure(
            resume_error,
            operation="resume",
            sandbox_id=sandbox_id,
        )
        logger.warning(f"Explicit sandbox resume failed for {sandbox_id}: {normalized_error}")
        return False, normalized_error
    return False, None


async def get_or_start_sandbox(sandbox_id: str, sandbox_type: str = 'desktop'):
    """Retrieve a sandbox by ID and attach with the most compatible SDK path."""

    logger.info(f"Getting or starting {sandbox_type} sandbox with ID: {sandbox_id}")

    def _load_sandbox_class(target_sandbox_type: str):
        if target_sandbox_type == 'desktop':
            try:
                from e2b_desktop import Sandbox  # type: ignore
            except ImportError:
                raise ImportError("e2b-desktop not installed. Run: pip install e2b-desktop")
            return Sandbox

        if target_sandbox_type in ['browser', 'code', 'base']:
            try:
                from e2b_code_interpreter import Sandbox  # type: ignore
            except ImportError:
                raise ImportError("e2b-code-interpreter not installed. Run: pip install e2b-code-interpreter")
            return Sandbox

        raise ValueError(f"Unsupported sandbox_type: {target_sandbox_type}")

    def _call_connect(connect_callable, target_sandbox_id: str, timeout_seconds: int):
        attempts = (
            lambda: connect_callable(sandbox_id=target_sandbox_id, timeout=timeout_seconds),
            lambda: connect_callable(id=target_sandbox_id, timeout=timeout_seconds),
            lambda: connect_callable(target_sandbox_id, timeout=timeout_seconds),
            lambda: connect_callable(sandbox_id=target_sandbox_id),
            lambda: connect_callable(id=target_sandbox_id),
            lambda: connect_callable(target_sandbox_id),
        )
        last_type_error = None
        for candidate in attempts:
            try:
                return candidate()
            except TypeError as type_error:
                last_type_error = type_error
                continue
        if last_type_error:
            raise last_type_error
        raise RuntimeError("Sandbox connect callable could not be invoked")

    def _constructor_attach(sandbox_cls, target_sandbox_id: str, target_sandbox_type: str):
        if target_sandbox_type in ['code', 'base', 'browser']:
            return sandbox_cls(sandbox_id=target_sandbox_id)

        import inspect

        sig = inspect.signature(sandbox_cls.__init__)
        params = list(sig.parameters.keys())

        if 'sandbox_id' in params:
            return sandbox_cls(sandbox_id=target_sandbox_id)
        if 'id' in params:
            return sandbox_cls(id=target_sandbox_id)

        sandbox = sandbox_cls()
        if hasattr(sandbox, 'sandbox_id'):
            sandbox.sandbox_id = target_sandbox_id
        elif hasattr(sandbox, 'id'):
            sandbox.id = target_sandbox_id
        return sandbox

    try:
        Sandbox = _load_sandbox_class(sandbox_type)

        connect_method = getattr(Sandbox, 'connect', None)
        if callable(connect_method):
            try:
                connected = await asyncio.to_thread(
                    _call_connect,
                    connect_method,
                    sandbox_id,
                    _get_resume_timeout_seconds(),
                )
                import inspect

                if inspect.isawaitable(connected):
                    connected = await connected
                if connected is not None:
                    logger.info(f"Sandbox {sandbox_id} connected via Sandbox.connect")
                    return connected
            except Exception as connect_error:
                normalized_connect_error = _coerce_sandbox_provider_failure(
                    connect_error,
                    operation="attach",
                    sandbox_id=sandbox_id,
                    sandbox_type=sandbox_type,
                )
                logger.warning(
                    f"Sandbox.connect attach failed for {sandbox_id} ({sandbox_type}), "
                    f"falling back to constructor attach: {normalized_connect_error}"
                )

        try:
            sandbox = await asyncio.to_thread(
                _constructor_attach,
                Sandbox,
                sandbox_id,
                sandbox_type,
            )
        except Exception as create_error:
            normalized_error = _coerce_sandbox_provider_failure(
                create_error,
                operation="attach",
                sandbox_id=sandbox_id,
                sandbox_type=sandbox_type,
            )
            logger.warning(f"Failed to connect to sandbox {sandbox_id}: {normalized_error}")
            logger.warning(f"Creation error type: {type(normalized_error).__name__}")
            logger.warning(f"Creation error details: {str(normalized_error)}")
            raise normalized_error

        logger.info(f"Sandbox {sandbox_id} is ready")
        return sandbox

    except Exception as e:
        normalized_error = _coerce_sandbox_provider_failure(
            e,
            operation="attach",
            sandbox_id=sandbox_id,
            sandbox_type=sandbox_type,
        )
        logger.error(f"Error retrieving or starting sandbox: {str(normalized_error)}")
        raise normalized_error


async def _set_sandbox_timeout_on_handle(
    sandbox_obj,
    *,
    sandbox_id: str,
    timeout_window_seconds: int,
    connect_timeout_seconds: float,
) -> bool:
    set_timeout_callable = getattr(sandbox_obj, "set_timeout", None)
    if not callable(set_timeout_callable):
        logger.warning("Sandbox %s does not expose set_timeout", sandbox_id)
        return False

    try:
        set_timeout_result = await asyncio.to_thread(
            set_timeout_callable,
            timeout_window_seconds,
        )
    except Exception as timeout_error:
        raise _coerce_sandbox_provider_failure(
            timeout_error,
            operation="timeout",
            sandbox_id=sandbox_id,
        ) from timeout_error
    if inspect.isawaitable(set_timeout_result):
        try:
            await asyncio.wait_for(
                set_timeout_result,
                timeout=connect_timeout_seconds,
            )
        except Exception as timeout_error:
            raise _coerce_sandbox_provider_failure(
                timeout_error,
                operation="timeout",
                sandbox_id=sandbox_id,
            ) from timeout_error
    logger.info(
        "Extended sandbox timeout for %s window=%ss",
        sandbox_id,
        timeout_window_seconds,
    )
    return True


async def extend_sandbox_timeout(
    sandbox_id: str,
    sandbox_type: str = "desktop",
    *,
    timeout_window_seconds: int | None = None,
    connect_timeout_seconds: float | None = None,
    require_running: bool = True,
    run_id: str | None = None,
):
    effective_window_seconds = timeout_window_seconds or _get_timeout_lease_window_seconds()
    effective_connect_timeout = (
        connect_timeout_seconds or _get_timeout_lease_connect_timeout_seconds()
    )
    shared_state = get_shadow_clone_session_state(run_id) if run_id else None
    shared_handle = getattr(shared_state, "sandbox", None) if shared_state is not None else None
    shared_sandbox_id = str(getattr(shared_state, "sandbox_id", None) or "").strip()
    if shared_handle is not None and (not shared_sandbox_id or shared_sandbox_id == sandbox_id):
        resolved_id = shared_sandbox_id or sandbox_id
        try:
            if require_running:
                is_running = await _probe_sandbox_running(
                    shared_handle,
                    sandbox_id=resolved_id,
                    timeout_seconds=min(
                        effective_connect_timeout,
                        _get_reattach_probe_timeout_seconds(),
                    ),
                )
                if not is_running:
                    raise RuntimeError(
                        f"Sandbox {resolved_id} is not running while extending timeout"
                    )
            if await _set_sandbox_timeout_on_handle(
                shared_handle,
                sandbox_id=resolved_id,
                timeout_window_seconds=effective_window_seconds,
                connect_timeout_seconds=effective_connect_timeout,
            ):
                return shared_handle
        except Exception as shared_timeout_error:
            logger.warning(
                "Shared Shadow Clone timeout renewal failed for run %s sandbox %s: %s",
                run_id,
                resolved_id,
                shared_timeout_error,
            )
            shared_state.clear_runtime_handle()

    sandbox_obj = await asyncio.wait_for(
        get_or_start_sandbox(sandbox_id, sandbox_type),
        timeout=effective_connect_timeout,
    )
    resolved_id = str(
        getattr(sandbox_obj, "sandbox_id", None)
        or getattr(sandbox_obj, "id", None)
        or sandbox_id
    ).strip() or sandbox_id
    if require_running:
        is_running = await _probe_sandbox_running(
            sandbox_obj,
            sandbox_id=resolved_id,
            timeout_seconds=min(
                effective_connect_timeout,
                _get_reattach_probe_timeout_seconds(),
            ),
        )
        if not is_running:
            raise RuntimeError(
                f"Sandbox {resolved_id} is not running while extending timeout"
            )
    if not await _set_sandbox_timeout_on_handle(
        sandbox_obj,
        sandbox_id=resolved_id,
        timeout_window_seconds=effective_window_seconds,
        connect_timeout_seconds=effective_connect_timeout,
    ):
        raise RuntimeError(f"Sandbox {resolved_id} does not support timeout renewal")
    return sandbox_obj

async def start_supervisord_session(sandbox):
    """Start supervisord in a session."""
    session_id = "supervisord-session"
    try:
        logger.info(f"Creating session {session_id} for supervisord")
        
        # 在 PPIO/E2B 中使用 commands.run 执行命令
        # 首先检查 supervisord 是否已经运行
        check_result = await asyncio.to_thread(
            sandbox.commands.run,
            "pgrep supervisord || echo 'not_running'",
        )
        
        if 'not_running' in check_result.stdout:
            # 启动 supervisord
            await asyncio.to_thread(
                sandbox.commands.run,
                "exec /usr/bin/supervisord -n -c /etc/supervisor/conf.d/supervisord.conf &",
            )
            logger.info(f"Supervisord started in session {session_id}")
        else:
            logger.info("Supervisord is already running")
            
    except Exception as e:
        logger.error(f"Error starting supervisord session: {str(e)}")
        raise e

async def create_sandbox(password: str, project_id: str = None, sandbox_type: str = 'desktop'):
    """
    Create a new sandbox with all required services configured and running.
    
    Args:
        password: VNC 密码
        project_id: 项目ID  
        sandbox_type: 沙箱类型 ('desktop', 'browser', 'code', 'base')
    """
    
    # https://ppio.com/docs/sandbox/e2b-sandbox
    logger.debug(f"Creating new PPIP/E2B sandbox environment with type: {sandbox_type}")
    logger.debug("Configuring sandbox with template and environment variables")
    
    # 获取对应的模板ID
    template_id = config.get_sandbox_template(sandbox_type)
    logger.info(f"Using template: {template_id} for type: {sandbox_type}")

    # 构建要注入沙箱的环境变量
    sandbox_envs = {}
    if hasattr(config, 'NETLIFY_AUTH_TOKEN') and config.NETLIFY_AUTH_TOKEN:
        sandbox_envs['NETLIFY_AUTH_TOKEN'] = config.NETLIFY_AUTH_TOKEN
        logger.debug("NETLIFY_AUTH_TOKEN configured for sandbox")
    if hasattr(config, 'NETLIFY_TEAM_SLUG') and config.NETLIFY_TEAM_SLUG:
        sandbox_envs['NETLIFY_TEAM_SLUG'] = config.NETLIFY_TEAM_SLUG
        logger.debug("NETLIFY_TEAM_SLUG configured for sandbox")
    # DashScope 视觉模型环境变量
    if hasattr(config, 'DASHSCOPE_API_KEY') and config.DASHSCOPE_API_KEY:
        sandbox_envs['DASHSCOPE_API_KEY'] = config.DASHSCOPE_API_KEY
        logger.debug("DASHSCOPE_API_KEY configured for sandbox")
    if hasattr(config, 'DASHSCOPE_BASE_URL') and config.DASHSCOPE_BASE_URL:
        sandbox_envs['DASHSCOPE_BASE_URL'] = config.DASHSCOPE_BASE_URL
        logger.debug("DASHSCOPE_BASE_URL configured for sandbox")
    if hasattr(config, 'DASHSCOPE_VISION_MODEL') and config.DASHSCOPE_VISION_MODEL:
        sandbox_envs['DASHSCOPE_VISION_MODEL'] = config.DASHSCOPE_VISION_MODEL
        logger.debug("DASHSCOPE_VISION_MODEL configured for sandbox")

    # 准备元数据，用于沙箱管理和查询
    # PPIO E2B metadata 中所有值必须是字符串类型
    try:
        # 确保 sandbox_type 是字符串
        sandbox_type_str = str(sandbox_type) if sandbox_type else 'desktop'
        
        metadata = {
            # 基础信息
            'project_type': 'chrome_vnc',
            'created_by': 'agent_system',
            'version': '1.0',
            'sandbox_type': sandbox_type_str,
            'template_id': str(template_id),
            'template_type': sandbox_type_str,
            'template_source': 'config_env',
            }

        # 启用 provider 侧闲时释放，减少无人访问时的运行成本。
        idle_timeout_seconds = getattr(config, 'SANDBOX_IDLE_TIMEOUT_SECONDS', 3600)
        try:
            idle_timeout_int = max(60, int(idle_timeout_seconds))
        except (TypeError, ValueError):
            idle_timeout_int = 3600
        metadata['idle_timeout'] = str(idle_timeout_int)
        
        # Chrome/VNC 配置摘要 - 转换为 JSON 字符串
        try:
            chrome_config = {
                'persistent_session': True,
                'resolution': '1024x768x24', 
                'debugging_port': '9222',
                'vnc_enabled': True
            }
            metadata['chrome_config'] = json.dumps(chrome_config)
        except Exception as chrome_error:
            metadata['chrome_config'] = '{}'
        
        # 资源配置 - 转换为 JSON 字符串
        try:
            resources_config = {
                'cpu': 2,
                'memory': 4,
                'disk': 5
            }
            metadata['resources'] = json.dumps(resources_config)
        except Exception as resources_error:
            metadata['resources'] = '{}'
        
        # 标签 - 转换为逗号分隔的字符串
        try:
            tag_list = ['chrome', 'vnc', 'agent_sandbox', sandbox_type_str]
            metadata['tags'] = ','.join(tag_list)
        except Exception as tags_error:
            metadata['tags'] = 'chrome,vnc,agent_sandbox'
        
        if project_id:
            metadata['project_id'] = str(project_id)
                    
    except Exception as metadata_error:
        logger.error(f"sandbox_type: {sandbox_type} (type: {type(sandbox_type)})")
        logger.error(f"project_id: {project_id} (type: {type(project_id) if project_id else None})")
        raise
        
    create_timeout_seconds = idle_timeout_int

    async def _instantiate_sandbox(sandbox_cls):
        max_attempts = _get_create_retry_max_attempts()
        base_backoff_seconds = _get_create_retry_base_backoff_seconds()
        max_backoff_seconds = _get_create_retry_max_backoff_seconds()
        normalized_project_id = str(project_id or "").strip() or "<unknown>"

        for attempt in range(1, max_attempts + 1):
            await sandbox_create_capacity.wait_for_sandbox_create_start_slot(
                project_id=normalized_project_id,
                sandbox_type=sandbox_type,
            )
            try:
                return await asyncio.to_thread(
                    sandbox_cls,
                    template=template_id,
                    timeout=create_timeout_seconds,
                    metadata=metadata,
                    envs=sandbox_envs if sandbox_envs else None,
                )
            except Exception as create_error:
                normalized_error = _coerce_sandbox_provider_failure(
                    create_error,
                    operation="create",
                    sandbox_type=sandbox_type,
                )
                logger.error(f"Failed to create {sandbox_type} sandbox: {normalized_error}")
                logger.error(f"Exception type: {type(normalized_error)}")
                if (
                    not isinstance(normalized_error, SandboxProviderFailure)
                    or not normalized_error.retryable
                    or attempt >= max_attempts
                ):
                    raise normalized_error from create_error

                backoff_seconds = min(
                    max_backoff_seconds,
                    base_backoff_seconds * (2 ** (attempt - 1)),
                )
                logger.warning(
                    "Retryable sandbox create failure for project %s type=%s attempt=%s/%s "
                    "reason=%s retrying_in=%.2fs",
                    normalized_project_id,
                    sandbox_type,
                    attempt,
                    max_attempts,
                    getattr(normalized_error, "provider_reason", None),
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)

    # 创建沙盒 - PPIO 使用正确的关键字参数
    logger.info(f"Creating sandbox with template: {template_id}")
    
    # 根据沙箱类型选择合适的 SDK 和创建方式
    logger.info(f"Creating {sandbox_type} sandbox with template: {template_id}")
    
    if sandbox_type == 'desktop':
        # Computer Use - 使用 e2b-desktop
        try:
            from e2b_desktop import Sandbox  # type: ignore
            logger.debug("Successfully imported e2b_desktop.Sandbox")
        except ImportError as e:
            logger.error(f"Failed to import e2b-desktop: {e}")
            raise ImportError("e2b-desktop not installed. Run: pip install e2b-desktop")
        
        try:
            logger.info(f"Creating desktop sandbox with:")
            logger.info(f"  template={template_id} (type: {type(template_id)})")
            logger.info(f"  timeout={create_timeout_seconds}")
            logger.info(f"  metadata keys={list(metadata.keys())}")
            logger.debug(f"  metadata content={metadata}")
            
            # 验证所有参数类型
            if not isinstance(template_id, str):
                raise TypeError(f"template_id must be string, got {type(template_id)}: {template_id}")
            
            for key, value in metadata.items():
                if not isinstance(key, str):
                    raise TypeError(f"metadata key must be string, got {type(key)}: {key}")
                if not isinstance(value, str):
                    raise TypeError(f"metadata[{key}] must be string, got {type(value)}: {value}")
            
            # 创建沙箱
            sandbox = await _instantiate_sandbox(Sandbox)
            logger.info(f"Desktop sandbox created successfully")
            logger.info(f"Sandbox object type: {type(sandbox)}")
            logger.info(f"Sandbox ID: {sandbox.sandbox_id if hasattr(sandbox, 'sandbox_id') else 'no sandbox_id attribute'}")
            
            # 启动桌面流
            try:
                logger.info("Starting desktop stream for VNC access...")
                await asyncio.to_thread(sandbox.stream.start)
                logger.info("Desktop stream started successfully")

                url = await asyncio.to_thread(sandbox.stream.get_url)
                logger.info(f"Desktop stream URL: {url}")

                # 自动关闭 XFCE 通知区域警告弹窗并禁用通知插件
                try:
                    logger.info("Fixing XFCE notification area issue...")
                    await asyncio.sleep(3)  # 等待桌面完全加载

                    # 方法1: 直接关闭当前弹窗
                    await asyncio.to_thread(
                        sandbox.commands.run,
                        "DISPLAY=:99 xdotool search --name 'notification' windowactivate --sync key Escape 2>/dev/null || "
                        "DISPLAY=:99 xdotool search --class 'xfce4-notifyd' key Escape 2>/dev/null || "
                        "DISPLAY=:99 wmctrl -c 'notification' 2>/dev/null || "
                        "echo 'dialog closed or not found'"
                    )

                    # 方法2: 永久禁用 XFCE 通知插件（治本）
                    await asyncio.to_thread(
                        sandbox.commands.run,
                        "xfconf-query -c xfce4-panel -p /plugins/plugin-6 -s '' 2>/dev/null || "
                        "killall xfce4-notifyd 2>/dev/null || "
                        "true"
                    )

                    logger.info("XFCE notification area issue fixed")
                except Exception as close_error:
                    logger.debug(f"Could not fix notification dialog: {close_error}")
                    # 忽略错误，这只是用户体验优化

            except Exception as stream_error:
                logger.warning(f"Failed to start desktop stream: {stream_error}")
                # 不阻止沙箱创建，可能在后续获取链接时再试
            
        except Exception as e:
            logger.error(f"Failed to create desktop sandbox: {e}")
            logger.error(f"Exception type: {type(e)}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            raise

    elif sandbox_type == 'browser':
        # Browser Use - 使用 e2b-code-interpreter
        try:
            from e2b_code_interpreter import Sandbox  # type: ignore
        except ImportError:
            raise ImportError("e2b-code-interpreter not installed. Run: pip install e2b-code-interpreter")
            
        sandbox = await _instantiate_sandbox(Sandbox)
        try:
            logger.info("Verifying Chrome debugging protocol availability...")
            # 验证 9223 端口是否可用 (Chrome 调试协议端口)
            chrome_host = await asyncio.to_thread(sandbox.get_host, 9223)
            cdp_url = f"https://{chrome_host}"
            logger.info(f"Chrome debugging protocol address available: {cdp_url}")
    
            # 可以添加更多浏览器相关的验证
            logger.info("Browser sandbox initialized successfully")
            
        except Exception as e:
            logger.warning(f"Browser sandbox initialization warning: {e}")
            # 不阻止沙箱创建，浏览器可能需要一些时间启动
            
    elif sandbox_type in ['code', 'base']:
        # 普通沙箱 - 使用 e2b-code-interpreter
        try:
            from e2b_code_interpreter import Sandbox  # type: ignore
        except ImportError:
            raise ImportError("e2b-code-interpreter not installed. Run: pip install e2b-code-interpreter")
            
        sandbox = await _instantiate_sandbox(Sandbox)
        logger.info("Base/Code sandbox initialized successfully")
        
    else:
        raise ValueError(f"Unsupported sandbox_type: {sandbox_type}. Supported types: desktop, browser, code, base")
    
    # 设置环境变量
    try:
        await setup_environment_variables(sandbox, password)
    except Exception as env_error:
        logger.warning(f"环境变量设置失败: {env_error}")
        # 继续执行，环境变量设置失败不应该阻止沙箱创建
    
    # 启动 supervisord
    try:
        await start_supervisord_session(sandbox)
    except Exception as supervisord_error:
        logger.warning(f"Supervisord start failed: {supervisord_error}")
        # 继续执行，supervisord 启动失败不应该阻止沙箱创建
    
    logger.debug(f"Sandbox environment successfully initialized")
    return sandbox

async def setup_environment_variables(sandbox, password: str):
    """设置沙箱环境变量"""
    logger.debug("Setting up environment variables")
    
    # 环境变量配置
    env_vars = {
        "CHROME_PERSISTENT_SESSION": "true", # Chrome 持久化会话配置：开启
        "RESOLUTION": "1024x768x24",  # VNC 远程桌面配置：完整分辨率（宽高像素）
        "RESOLUTION_WIDTH": "1024",  # VNC 远程桌面配置：宽度像素
        "RESOLUTION_HEIGHT": "768",  # VNC 远程桌面配置：高度像素
        "VNC_PASSWORD": password,  # VNC 远程桌面配置：密码
        "ANONYMIZED_TELEMETRY": "false",  # 匿名化遥测配置：关闭
        "CHROME_PATH": "",  # Chrome 路径
        "CHROME_USER_DATA": "",  # Chrome 用户数据路径
        "CHROME_DEBUGGING_PORT": "9222",  # Chrome 调试端口
        "CHROME_DEBUGGING_HOST": "localhost",  # Chrome 调试主机
        "CHROME_CDP": "",  # Chrome CDP 配置
        "DASHSCOPE_API_KEY": config.DASHSCOPE_API_KEY or "",
        "DASHSCOPE_BASE_URL": config.DASHSCOPE_BASE_URL or "",
        "DASHSCOPE_VISION_MODEL": config.DASHSCOPE_VISION_MODEL or "",
    }
    
    # 通过 commands.run 设置环境变量
    for key, value in env_vars.items():
        try:
            # 确保 key 和 value 都是字符串
            key_str = str(key)
            value_str = str(value) if value is not None else ""
            
            
            # 设置当前会话的环境变量
            export_cmd = f'export {key_str}="{value_str}"'
            await asyncio.to_thread(sandbox.commands.run, export_cmd)
            
            # 添加到 .bashrc 以持久化
            bashrc_cmd = f'echo \'export {key_str}="{value_str}"\' >> ~/.bashrc'
            await asyncio.to_thread(sandbox.commands.run, bashrc_cmd)

            
        except Exception as e:
            logger.warning(f"Failed to set environment variable {key}: {e}")
            logger.debug(f"Key type: {type(key)}, Value type: {type(value)}")
            logger.debug(f"Key: {key}, Value: {value}")
    
    # 重新加载 .bashrc
    try:
        await asyncio.to_thread(sandbox.commands.run, 'source ~/.bashrc')
        logger.debug("Environment variables configured successfully")
    except Exception as e:
        logger.warning(f"Failed to reload .bashrc: {e}")

async def delete_sandbox(sandbox_id: str):
    """Delete a sandbox by its ID."""
    logger.info(f"Deleting sandbox with ID: {sandbox_id}")
    delete_timeout_seconds = _get_delete_timeout_seconds()

    # 尝试不同的 SDK，因为不知道具体类型
    sdks_to_try = [
        ('e2b_desktop', 'e2b-desktop'),
        ('e2b_code_interpreter', 'e2b-code-interpreter')
    ]
    
    for sdk_module, sdk_name in sdks_to_try:
        try:
            logger.debug(f"Trying to delete sandbox {sandbox_id} using {sdk_name}")
            
            # 动态导入对应的 SDK
            if sdk_module == 'e2b_desktop':
                from e2b_desktop import Sandbox  # type: ignore
            elif sdk_module == 'e2b_code_interpreter':
                from e2b_code_interpreter import Sandbox  # type: ignore
                                
            # 尝试连接并删除
            try:
                sandbox = await asyncio.wait_for(
                    asyncio.to_thread(Sandbox, sandbox_id=sandbox_id),
                    timeout=delete_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out attaching to sandbox %s using %s after %.1fs",
                    sandbox_id,
                    sdk_name,
                    delete_timeout_seconds,
                )
                continue

            try:
                await asyncio.wait_for(
                    asyncio.to_thread(sandbox.kill),
                    timeout=delete_timeout_seconds,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Timed out deleting sandbox %s using %s after %.1fs",
                    sandbox_id,
                    sdk_name,
                    delete_timeout_seconds,
                )
                continue
            
            logger.info(f"Successfully deleted sandbox {sandbox_id} using {sdk_name}")
            return True
        
        except ImportError:
            logger.debug(f"SDK {sdk_name} not available, skipping")
            continue
        except Exception as e:
            if _is_sandbox_not_found_error(e):
                logger.info(
                    "Sandbox %s already deleted or missing while using %s",
                    sandbox_id,
                    sdk_name,
                )
                return True
            logger.debug(f"Failed to delete sandbox {sandbox_id} using {sdk_name}: {e}")
            continue
    
    # 如果所有 SDK 都失败了
    raise Exception(f"Failed to delete sandbox {sandbox_id} with all available SDKs")

async def pause_sandbox(sandbox_id: str) -> bool:
    """Pause a sandbox by ID using PPIO SDK. Returns True on success."""
    logger.info(f"Pausing sandbox {sandbox_id}")
    try:
        from ppio_sandbox.core import Sandbox as PPIOSandbox
        await asyncio.to_thread(PPIOSandbox._cls_pause, sandbox_id)
        logger.info(f"Successfully paused sandbox {sandbox_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to pause sandbox {sandbox_id}: {e}")
        return False


async def clone_sandbox(sandbox_id: str, count: int = 1, timeout: int = 3600) -> tuple:
    """Clone a paused/running sandbox. Returns (new_sandbox_id, snapshot_template_id)."""
    logger.info(f"Cloning sandbox {sandbox_id} (count={count})")
    try:
        from ppio_sandbox.core import Sandbox as PPIOSandbox
        result = await asyncio.to_thread(
            PPIOSandbox.clone, sandbox_id, count, timeout=timeout
        )
        new_sbx = result.sandboxes[0]
        new_id = new_sbx.sandbox_id
        logger.info(f"Successfully cloned sandbox {sandbox_id} -> {new_id}")
        return (new_id, result.snapshot_template_id)
    except Exception as e:
        logger.error(f"Failed to clone sandbox {sandbox_id}: {e}")
        raise


async def resume_or_create_sandbox(
    password: str,
    project_id: str,
    sandbox_type: str = 'desktop',
    sandbox_info: dict = None,
    reattach_budget_seconds: float = None,
    allow_create: bool = True,
) -> tuple:
    """Try to resume a sandbox by id; fall back to creating new.
    Returns (sandbox_obj, action) where action is 'resumed' or 'created'."""
    sandbox_info = sandbox_info or {}
    old_id = str(sandbox_info.get('id') or '').strip()
    last_error = None

    if old_id:
        # Explicit resume is best-effort and helps when provider state drifted
        # (e.g. DB says running but sandbox is actually paused).
        budget_seconds = _get_reattach_budget_seconds()
        if reattach_budget_seconds is not None:
            budget_seconds = _safe_positive_float(reattach_budget_seconds, budget_seconds)
        probe_timeout_seconds = _get_reattach_probe_timeout_seconds()
        resume_nudge_timeout_seconds = min(
            budget_seconds,
            _get_resume_nudge_timeout_seconds(),
        )

        if resume_nudge_timeout_seconds > 0:
            resume_ok, resume_error = await _attempt_ppio_resume(
                old_id,
                timeout_seconds=resume_nudge_timeout_seconds,
            )
            if not resume_ok and isinstance(resume_error, SandboxProviderFailure):
                last_error = resume_error
                if not allow_create and not resume_error.retryable:
                    raise SandboxOriginalReuseExhausted(
                        old_id,
                        sandbox_type,
                        project_id,
                        last_error=resume_error,
                    )

        # Short retry schedule within budget to maximize old-ID recovery without long user wait.
        attempt_offsets = (0.0, 0.25, 0.75, 1.5, 2.5, 4.0)
        started = time.monotonic()

        for attempt, offset_seconds in enumerate(attempt_offsets, start=1):
            elapsed = time.monotonic() - started
            if elapsed >= budget_seconds:
                break

            wait_seconds = max(0.0, min(offset_seconds, budget_seconds) - elapsed)
            if wait_seconds > 0:
                await asyncio.sleep(wait_seconds)

            try:
                sandbox_obj = await get_or_start_sandbox(old_id, sandbox_type)
            except Exception as reattach_error:
                normalized_reattach_error = _coerce_sandbox_provider_failure(
                    reattach_error,
                    operation="attach",
                    sandbox_id=old_id,
                    sandbox_type=sandbox_type,
                )
                last_error = normalized_reattach_error
                logger.warning(
                    f"Sandbox reattach attempt {attempt} failed for {old_id}: {normalized_reattach_error}"
                )
                if (
                    not allow_create
                    and isinstance(normalized_reattach_error, SandboxProviderFailure)
                    and not normalized_reattach_error.retryable
                ):
                    break
                continue

            is_alive = await _probe_sandbox_running(
                sandbox_obj,
                sandbox_id=old_id,
                timeout_seconds=probe_timeout_seconds,
            )
            if is_alive:
                logger.info(
                    f"Reattached sandbox {old_id} for project {project_id} "
                    f"(state={sandbox_info.get('state')}, attempts={attempt}, "
                    f"elapsed_ms={int((time.monotonic() - started) * 1000)})"
                )
                return (sandbox_obj, 'resumed')

            last_error = RuntimeError("is_running returned False")
            logger.warning(
                f"Sandbox reattach attempt {attempt} produced non-running handle for {old_id}"
            )

        logger.warning(
            f"Sandbox {old_id} was not recoverable within {budget_seconds:.2f}s "
            f"(project={project_id}, last_error={last_error})"
        )
    elif not allow_create:
        raise SandboxOriginalReuseExhausted(
            "",
            sandbox_type,
            project_id,
            last_error=RuntimeError("original sandbox id missing from metadata"),
        )

    if not allow_create:
        raise SandboxOriginalReuseExhausted(
            old_id,
            sandbox_type,
            project_id,
            last_error=last_error,
        )

    sandbox_obj = await create_sandbox(password, project_id, sandbox_type)
    new_id = str(getattr(sandbox_obj, "sandbox_id", None) or getattr(sandbox_obj, "id", ""))
    create_alive = await _probe_sandbox_running(
        sandbox_obj,
        sandbox_id=new_id or old_id or "new",
        timeout_seconds=_get_reattach_probe_timeout_seconds(),
    )
    if not create_alive:
        raise RuntimeError(
            f"Created sandbox {new_id or '<unknown>'} is not running after recovery"
        )
    logger.info(
        f"Created new sandbox {new_id or getattr(sandbox_obj, 'sandbox_id', '<unknown>')} "
        f"for project {project_id}"
    )
    return (sandbox_obj, 'created')

async def get_sandbox_metrics(sandbox):
    """获取沙箱资源使用指标"""
    try:
        metrics = await sandbox.getMetrics()
        logger.debug(f"Sandbox metrics: {metrics}")
        return metrics
    except Exception as e:
        logger.warning(f"Failed to get sandbox metrics: {e}")
        return None

async def list_sandboxes(metadata_filter: dict = None):
    """列出所有沙箱 - PPIO 暂不支持复杂查询，返回空列表"""
    try:
        # PPIO 的 E2B SDK 可能不支持复杂的列表查询
        # 这里返回空列表，实际使用中可能需要其他方式管理沙箱列表
        logger.warning("PPIO sandbox listing not fully supported, returning empty list")
        return []
    except Exception as e:
        logger.error(f"Error listing sandboxes: {e}")
        return []

# 元数据的实际使用示例
async def find_chrome_sandboxes():
    """查找所有 Chrome 类型的沙箱"""
    return await list_sandboxes({'project_type': 'chrome_vnc'})

async def find_project_sandboxes(project_id: str):
    """查找特定项目的沙箱"""
    return await list_sandboxes({'project_id': project_id})

async def find_agent_sandboxes():
    """查找所有 agent 创建的沙箱"""
    return await list_sandboxes({'created_by': 'agent_system'})

async def get_sandbox_config_summary(sandbox):
    """从元数据快速获取配置摘要"""
    try:
        info = await sandbox.getInfo()
        metadata = info.get('metadata', {})
        
        return {
            'type': metadata.get('project_type', 'unknown'),
            'chrome_config': metadata.get('chrome_config', {}),
            'resources': metadata.get('resources', {}),
            'tags': metadata.get('tags', [])
        }
    except Exception as e:
        logger.warning(f"Failed to get config summary: {e}")
        return None
