"""Factory for building long-term memory sidecar instances."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Optional

from agentscope.embedding import OpenAITextEmbedding
from agentscope.memory import ReMeTaskLongTermMemory, ReMeToolLongTermMemory
from agentscope.model import OpenAIChatModel

from utils.logger import logger

from .composite_ltm import CompositeLongTermMemory
from .ltm_types import (
    DEFAULT_DASHSCOPE_COMPAT_BASE_URL,
    LTMSettings,
    VALID_LTM_CONTROL_MODES,
    normalize_memories,
)

_BACKEND_ROOT = Path(__file__).resolve().parents[3]
_CUSTOM_REME_OPS_REGISTERED = False


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_api_key() -> str:
    candidates = (
        "AGENTSCOPE_LTM_API_KEY",
        "DASHSCOPE_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
    )
    for key in candidates:
        value = str(os.environ.get(key, "")).strip()
        if value:
            return value
    return ""


def _resolve_api_base() -> str:
    candidates = (
        "AGENTSCOPE_LTM_API_BASE",
        "AGENTSCOPE_LTM_BASE_URL",
        "DASHSCOPE_COMPAT_BASE_URL",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "OPENROUTER_BASE_URL",
    )
    for key in candidates:
        value = str(os.environ.get(key, "")).strip()
        if value:
            return value
    return DEFAULT_DASHSCOPE_COMPAT_BASE_URL


def _resolve_reme_config_path(raw_path: str) -> str:
    value = str(raw_path or "").strip()
    if not value:
        return ""

    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        if candidate.exists():
            return str(candidate)
        logger.warning(
            "[LTMFactory] AGENTSCOPE_LTM_REME_CONFIG_PATH points to missing absolute path: %s",
            value,
        )
        return value

    cwd_candidate = (Path.cwd() / candidate).resolve()
    if cwd_candidate.exists():
        return str(cwd_candidate)

    backend_candidate = (_BACKEND_ROOT / candidate).resolve()
    if backend_candidate.exists():
        return str(backend_candidate)

    logger.warning(
        "[LTMFactory] Could not resolve AGENTSCOPE_LTM_REME_CONFIG_PATH=%s from cwd=%s or backend_root=%s",
        value,
        Path.cwd(),
        _BACKEND_ROOT,
    )
    return value


def _sanitize_reme_kwargs(raw_kwargs: dict) -> dict:
    cleaned = {}
    for key, value in (raw_kwargs or {}).items():
        if not isinstance(key, str):
            logger.warning(
                "[LTMFactory] Ignoring non-string key in AGENTSCOPE_LTM_REME_KWARGS_JSON: %r",
                key,
            )
            continue

        clean_key = key.strip()
        if not clean_key:
            continue

        # FlowLLM dotted keys are parsed from CLI-like strings. Nested dict/list
        # values must be JSON strings, otherwise they are stringified as Python dicts
        # and fail ServiceConfig validation.
        if "." in clean_key and isinstance(value, (dict, list)):
            cleaned[clean_key] = json.dumps(value, ensure_ascii=False)
            logger.warning(
                "[LTMFactory] Serialized nested override for dotted ReMe key=%s to JSON string for parser compatibility.",
                clean_key,
            )
            continue

        cleaned[clean_key] = value

    return cleaned


def _uses_qdrant_backend(settings: LTMSettings) -> bool:
    config_path = str(settings.reme_config_path or "").strip().lower()
    if "qdrant" in config_path:
        return True

    backend_override = str(
        (settings.reme_kwargs or {}).get("vector_store.default.backend", ""),
    ).strip().lower()
    return backend_override == "qdrant"


def _prepare_qdrant_vector_store(settings: LTMSettings) -> None:
    """Ensure FlowLLM Qdrant model forward refs are resolved for Pydantic v2."""
    if not _uses_qdrant_backend(settings):
        return

    try:
        from flowllm.core.vector_store.qdrant_vector_store import QdrantVectorStore
        from qdrant_client.http.models import Distance
    except Exception as exc:
        if settings.fail_open:
            logger.warning(
                "[LTMFactory] Qdrant preflight import failed (fail-open): %s",
                exc,
            )
            return
        raise

    try:
        rebuild_result = QdrantVectorStore.model_rebuild(
            _types_namespace={"Distance": Distance},
        )
        logger.info(
            "[LTMFactory] QdrantVectorStore model_rebuild applied result=%s",
            rebuild_result,
        )
    except Exception as exc:
        if settings.fail_open:
            logger.warning(
                "[LTMFactory] QdrantVectorStore model_rebuild failed (fail-open): %s",
                exc,
            )
            return
        raise


def _register_custom_reme_ops(settings: LTMSettings) -> None:
    """Register project-specific ReMe ops before ReMeApp is initialized."""
    global _CUSTOM_REME_OPS_REGISTERED
    if _CUSTOM_REME_OPS_REGISTERED:
        return

    try:
        # Import side-effect registers @C.register_op decorated ops.
        from . import fast_dedicated_rerank_op  # noqa: F401
    except Exception as exc:
        if settings.fail_open:
            logger.warning(
                "[LTMFactory] Custom ReMe op registration failed (fail-open): %s",
                exc,
            )
            return
        raise

    _CUSTOM_REME_OPS_REGISTERED = True
    logger.info(
        "[LTMFactory] Registered custom ReMe ops: DashScopeTextRerankOp",
    )


def _patch_async_qdrant_search_compat(settings: LTMSettings) -> None:
    """Patch AsyncQdrantClient.search for newer clients that only expose query_points."""
    if not _uses_qdrant_backend(settings):
        return

    try:
        from qdrant_client import AsyncQdrantClient
    except Exception as exc:
        if settings.fail_open:
            logger.warning(
                "[LTMFactory] Qdrant search-compat import failed (fail-open): %s",
                exc,
            )
            return
        raise

    if hasattr(AsyncQdrantClient, "search"):
        return

    if not hasattr(AsyncQdrantClient, "query_points"):
        message = (
            "[LTMFactory] AsyncQdrantClient has neither 'search' nor 'query_points'; "
            "cannot apply compatibility shim."
        )
        if settings.fail_open:
            logger.warning(message)
            return
        raise RuntimeError(message)

    async def _search(
        self,
        collection_name,
        query_vector,
        limit=10,
        query_filter=None,
        with_payload=True,
        with_vectors=False,
        **kwargs,
    ):
        response = await self.query_points(
            collection_name=collection_name,
            query=query_vector,
            limit=limit,
            query_filter=query_filter,
            with_payload=with_payload,
            with_vectors=with_vectors,
            **kwargs,
        )
        points = getattr(response, "points", None)
        if points is not None:
            return points

        result = getattr(response, "result", None)
        if result is None:
            return []
        if isinstance(result, list):
            return result
        return [result]

    setattr(AsyncQdrantClient, "search", _search)

    try:
        client_version = importlib_metadata.version("qdrant-client")
    except Exception:
        client_version = "unknown"

    logger.warning(
        "[LTMFactory] Applied AsyncQdrantClient.search compatibility shim via query_points "
        "(qdrant-client=%s).",
        client_version,
    )


def _sanitize_qdrant_distance_override(settings: LTMSettings) -> dict:
    """Drop explicit qdrant distance override to avoid flowllm runtime bug.

    In the current flowllm/qdrant-client combination, explicitly passing
    `distance` may trigger a KeyError during QdrantVectorStore init_client.
    We therefore keep distance unset and let the backend default apply.
    """
    kwargs = dict(settings.reme_kwargs or {})
    if not _uses_qdrant_backend(settings):
        return kwargs

    key = "vector_store.default.params.distance"
    if key in kwargs:
        raw_value = kwargs.pop(key)
        logger.warning(
            "[LTMFactory] Removed explicit qdrant distance override (%s=%r) to avoid "
            "known flowllm/qdrant init_client compatibility issue. Backend default applies.",
            key,
            raw_value,
        )
    return kwargs


def load_ltm_settings_from_env() -> LTMSettings:
    """Load long-term memory settings from environment variables."""

    retrieve_limit_raw = str(os.environ.get("AGENTSCOPE_LTM_RETRIEVE_LIMIT", "5")).strip()
    try:
        retrieve_limit = max(1, int(retrieve_limit_raw))
    except ValueError:
        retrieve_limit = 5

    embedding_dims_raw = str(
        os.environ.get("AGENTSCOPE_LTM_EMBEDDING_DIMENSIONS", "1024"),
    ).strip()
    try:
        embedding_dims = max(1, int(embedding_dims_raw))
    except ValueError:
        embedding_dims = 1024

    control_mode = str(os.environ.get("AGENTSCOPE_LTM_CONTROL", "both")).strip().lower()
    if control_mode not in VALID_LTM_CONTROL_MODES:
        control_mode = "both"

    raw_reme_kwargs = str(os.environ.get("AGENTSCOPE_LTM_REME_KWARGS_JSON", "")).strip()
    reme_kwargs = {}
    if raw_reme_kwargs:
        try:
            loaded = json.loads(raw_reme_kwargs)
            if isinstance(loaded, dict):
                reme_kwargs = _sanitize_reme_kwargs(loaded)
        except Exception as exc:
            logger.warning(
                "[LTMFactory] Failed to parse AGENTSCOPE_LTM_REME_KWARGS_JSON: %s",
                exc,
            )

    reme_config_path = _resolve_reme_config_path(
        str(os.environ.get("AGENTSCOPE_LTM_REME_CONFIG_PATH", "")).strip(),
    )

    task_query_mode = str(
        os.environ.get("AGENTSCOPE_LTM_TASK_QUERY_MODE", "merged"),
    ).strip().lower()
    if task_query_mode not in {"merged", "per_keyword"}:
        task_query_mode = "merged"

    task_query_keyword_limit_raw = str(
        os.environ.get("AGENTSCOPE_LTM_TASK_QUERY_KEYWORD_LIMIT", "8"),
    ).strip()
    try:
        task_query_keyword_limit = max(1, int(task_query_keyword_limit_raw))
    except ValueError:
        task_query_keyword_limit = 8

    settings = LTMSettings(
        enabled=_env_flag("AGENTSCOPE_LTM_ENABLED", False),
        control_mode=control_mode,
        attach_scope=str(os.environ.get("AGENTSCOPE_LTM_ATTACH_SCOPE", "orchestrator")).strip().lower() or "orchestrator",
        memories=normalize_memories(
            os.environ.get("AGENTSCOPE_LTM_MEMORIES", "task,tool"),
            default=("task", "tool"),
        ),
        fail_open=_env_flag("AGENTSCOPE_LTM_FAIL_OPEN", True),
        retrieve_limit=retrieve_limit,
        write_gate_enabled=_env_flag("AGENTSCOPE_LTM_WRITE_GATE_ENABLED", True),
        static_record_enabled=_env_flag("AGENTSCOPE_LTM_STATIC_RECORD_ENABLED", False),
        model_name=str(os.environ.get("AGENTSCOPE_LTM_MODEL", "qwen3.5-35b-a3b")).strip() or "qwen3.5-35b-a3b",
        embedding_model_name=str(os.environ.get("AGENTSCOPE_LTM_EMBEDDING_MODEL", "text-embedding-v4")).strip() or "text-embedding-v4",
        rerank_model_name=str(os.environ.get("AGENTSCOPE_LTM_RERANK_MODEL", "qwen3-vl-rerank")).strip() or "qwen3-vl-rerank",
        global_workspace=str(os.environ.get("AGENTSCOPE_LTM_GLOBAL_WORKSPACE", "global_task_tool_v1")).strip() or "global_task_tool_v1",
        api_key=_resolve_api_key(),
        api_base=_resolve_api_base(),
        embedding_dimensions=embedding_dims,
        reme_config_path=reme_config_path,
        reme_kwargs=reme_kwargs,
        task_query_mode=task_query_mode,
        task_query_keyword_limit=task_query_keyword_limit,
    )
    return settings


def _build_chat_model(settings: LTMSettings) -> OpenAIChatModel:
    return OpenAIChatModel(
        model_name=settings.model_name,
        api_key=settings.api_key,
        stream=False,
        client_kwargs={"base_url": settings.api_base},
    )


def _build_embedding_model(settings: LTMSettings) -> OpenAITextEmbedding:
    return OpenAITextEmbedding(
        api_key=settings.api_key,
        model_name=settings.embedding_model_name,
        dimensions=settings.embedding_dimensions,
        base_url=settings.api_base,
    )


def _build_reme_memory(
    memory_cls,
    *,
    workspace_id: str,
    thread_id: str,
    model: OpenAIChatModel,
    embedding_model: OpenAITextEmbedding,
    settings: LTMSettings,
):
    kwargs = dict(settings.reme_kwargs or {})
    kwargs.setdefault("rerank_model_name", settings.rerank_model_name)

    try:
        return memory_cls(
            agent_name="RoysAlpha-Orchestrator",
            user_name=workspace_id,
            run_name=thread_id,
            model=model,
            embedding_model=embedding_model,
            reme_config_path=settings.reme_config_path or None,
            **kwargs,
        )
    except TypeError as exc:
        if "rerank_model_name" in kwargs and "unexpected keyword" in str(exc).lower():
            logger.warning(
                "[LTMFactory] ReMe backend rejected rerank_model_name kwarg; retry without it.",
            )
            kwargs.pop("rerank_model_name", None)
            return memory_cls(
                agent_name="RoysAlpha-Orchestrator",
                user_name=workspace_id,
                run_name=thread_id,
                model=model,
                embedding_model=embedding_model,
                reme_config_path=settings.reme_config_path or None,
                **kwargs,
            )
        raise


def create_long_term_memory(
    *,
    thread_id: str,
    settings: Optional[LTMSettings] = None,
) -> Optional[CompositeLongTermMemory]:
    """Create composite long-term memory according to runtime settings."""

    cfg = settings or load_ltm_settings_from_env()
    if not cfg.enabled:
        return None

    if cfg.reme_config_path:
        logger.info(
            "[LTMFactory] Using explicit ReMe config path: %s",
            cfg.reme_config_path,
        )
    else:
        logger.warning(
            "[LTMFactory] AGENTSCOPE_LTM_REME_CONFIG_PATH is empty. ReMe default config applies "
            "(commonly vector_store=memory). Multi-worker shared memory requires a qdrant config path.",
        )

    if not cfg.api_key:
        raise RuntimeError(
            "LTM is enabled but no API key is configured. "
            "Set AGENTSCOPE_LTM_API_KEY or DASHSCOPE_API_KEY/OPENAI_API_KEY.",
        )

    if not cfg.is_valid_control_mode():
        raise RuntimeError(f"Invalid AGENTSCOPE_LTM_CONTROL: {cfg.control_mode}")

    if "personal" in set(cfg.memories):
        logger.info("[LTMFactory] personal memory is configured but disabled in v1 runtime path.")

    sanitized_reme_kwargs = _sanitize_qdrant_distance_override(cfg)
    if sanitized_reme_kwargs != dict(cfg.reme_kwargs or {}):
        cfg = replace(cfg, reme_kwargs=sanitized_reme_kwargs)
    _register_custom_reme_ops(cfg)
    _patch_async_qdrant_search_compat(cfg)
    _prepare_qdrant_vector_store(cfg)

    chat_model = _build_chat_model(cfg)
    embedding_model = _build_embedding_model(cfg)

    task_memory = None
    tool_memory = None
    workspace_id = cfg.global_workspace

    if cfg.has_memory("task"):
        task_memory = _build_reme_memory(
            ReMeTaskLongTermMemory,
            workspace_id=workspace_id,
            thread_id=thread_id,
            model=chat_model,
            embedding_model=embedding_model,
            settings=cfg,
        )
    if cfg.has_memory("tool"):
        tool_memory = _build_reme_memory(
            ReMeToolLongTermMemory,
            workspace_id=workspace_id,
            thread_id=thread_id,
            model=chat_model,
            embedding_model=embedding_model,
            settings=cfg,
        )

    if task_memory is None and tool_memory is None:
        return None

    qdrant_url = None
    if _uses_qdrant_backend(cfg):
        qdrant_host = str(os.environ.get("FLOW_QDRANT_HOST", "localhost")).strip()
        qdrant_port = str(os.environ.get("FLOW_QDRANT_PORT", "6333")).strip()
        qdrant_url = f"http://{qdrant_host}:{qdrant_port}"

    logger.info(
        "[LTMFactory] Created LTM sidecar memories=%s workspace=%s model=%s embedding=%s rerank=%s "
        "reme_config_path=%s reme_kwargs_keys=%s",
        ",".join(cfg.memories),
        workspace_id,
        cfg.model_name,
        cfg.embedding_model_name,
        cfg.rerank_model_name,
        cfg.reme_config_path or "<default>",
        ",".join(sorted((cfg.reme_kwargs or {}).keys())) or "<none>",
    )

    return CompositeLongTermMemory(
        task_memory=task_memory,
        tool_memory=tool_memory,
        retrieve_limit=cfg.retrieve_limit,
        write_gate_enabled=cfg.write_gate_enabled,
        static_record_enabled=cfg.static_record_enabled,
        fail_open=cfg.fail_open,
        task_query_mode=cfg.task_query_mode,
        task_query_keyword_limit=cfg.task_query_keyword_limit,
        qdrant_collection=workspace_id,
        qdrant_url=qdrant_url,
    )
