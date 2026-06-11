import asyncio
import json
import sys
import types
from pathlib import Path

if "structlog" not in sys.modules:
    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    dummy_structlog = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )
    sys.modules["structlog"] = dummy_structlog

if "services.postgresql" not in sys.modules:
    services_pkg = types.ModuleType("services")
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    postgresql_mod.DBConnection = _DummyDBConnection
    services_pkg.postgresql = postgresql_mod
    sys.modules["services"] = services_pkg
    sys.modules["services.postgresql"] = postgresql_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentscope_integration.memory.long_term.ltm_factory import (
    _patch_async_qdrant_search_compat,
    _sanitize_qdrant_distance_override,
    _prepare_qdrant_vector_store,
    _uses_qdrant_backend,
    load_ltm_settings_from_env,
)
from agentscope_integration.memory.long_term.ltm_types import LTMSettings


def test_load_ltm_settings_serializes_nested_dotted_reme_kwargs(monkeypatch):
    monkeypatch.setenv(
        "AGENTSCOPE_LTM_REME_KWARGS_JSON",
        json.dumps(
            {
                "vector_store.default.params": {
                    "url": "https://example-qdrant:6333",
                    "api_key": "qdrant-key",
                    "distance": "COSINE",
                },
                "llm.default.model_name": "qwen3.5-35b-a3b",
            },
        ),
    )

    settings = load_ltm_settings_from_env()

    nested_params = settings.reme_kwargs.get("vector_store.default.params")
    assert isinstance(nested_params, str)
    parsed = json.loads(nested_params)
    assert parsed["url"] == "https://example-qdrant:6333"
    assert parsed["distance"] == "COSINE"
    assert settings.reme_kwargs["llm.default.model_name"] == "qwen3.5-35b-a3b"


def test_load_ltm_settings_task_query_mode_defaults_and_sanitization(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_LTM_TASK_QUERY_MODE", "unsupported")
    monkeypatch.setenv("AGENTSCOPE_LTM_TASK_QUERY_KEYWORD_LIMIT", "not-a-number")

    settings = load_ltm_settings_from_env()

    assert settings.task_query_mode == "merged"
    assert settings.task_query_keyword_limit == 8


def test_load_ltm_settings_resolves_reme_config_path_from_backend_root(
    monkeypatch,
    tmp_path: Path,
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(
        "AGENTSCOPE_LTM_REME_CONFIG_PATH",
        "agentscope_integration/memory/long_term/reme_qdrant_shared.yaml",
    )

    settings = load_ltm_settings_from_env()
    resolved = Path(settings.reme_config_path)
    assert resolved.is_absolute()
    assert resolved.exists()
    assert resolved.name == "reme_qdrant_shared.yaml"


def test_uses_qdrant_backend_detects_from_reme_config_path():
    settings = LTMSettings(
        enabled=True,
        control_mode="both",
        attach_scope="orchestrator",
        memories=("task", "tool"),
        fail_open=True,
        retrieve_limit=5,
        write_gate_enabled=True,
        static_record_enabled=False,
        model_name="qwen3.5-35b-a3b",
        embedding_model_name="text-embedding-v4",
        rerank_model_name="qwen3-vl-rerank",
        global_workspace="global_task_tool_v1",
        api_key="dummy",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_dimensions=1024,
        reme_config_path="/tmp/reme_qdrant_shared.yaml",
        reme_kwargs={},
    )
    assert _uses_qdrant_backend(settings) is True


def test_uses_qdrant_backend_detects_from_reme_kwargs_override():
    settings = LTMSettings(
        enabled=True,
        control_mode="both",
        attach_scope="orchestrator",
        memories=("task", "tool"),
        fail_open=True,
        retrieve_limit=5,
        write_gate_enabled=True,
        static_record_enabled=False,
        model_name="qwen3.5-35b-a3b",
        embedding_model_name="text-embedding-v4",
        rerank_model_name="qwen3-vl-rerank",
        global_workspace="global_task_tool_v1",
        api_key="dummy",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_dimensions=1024,
        reme_config_path="",
        reme_kwargs={"vector_store.default.backend": "qdrant"},
    )
    assert _uses_qdrant_backend(settings) is True


def test_prepare_qdrant_vector_store_calls_model_rebuild(monkeypatch):
    class _DummyQdrantVectorStore:
        captured_types_namespace = None

        @classmethod
        def model_rebuild(cls, _types_namespace=None):
            cls.captured_types_namespace = _types_namespace
            return True

    class _DummyDistance:
        COSINE = "COSINE"

    # Provide minimal fake modules for the import path used by ltm_factory.
    flowllm_pkg = types.ModuleType("flowllm")
    flowllm_core_pkg = types.ModuleType("flowllm.core")
    flowllm_vector_store_pkg = types.ModuleType("flowllm.core.vector_store")
    qdrant_vs_mod = types.ModuleType("flowllm.core.vector_store.qdrant_vector_store")
    qdrant_vs_mod.QdrantVectorStore = _DummyQdrantVectorStore

    qdrant_pkg = types.ModuleType("qdrant_client")
    qdrant_http_pkg = types.ModuleType("qdrant_client.http")
    qdrant_models_mod = types.ModuleType("qdrant_client.http.models")
    qdrant_models_mod.Distance = _DummyDistance

    monkeypatch.setitem(sys.modules, "flowllm", flowllm_pkg)
    monkeypatch.setitem(sys.modules, "flowllm.core", flowllm_core_pkg)
    monkeypatch.setitem(sys.modules, "flowllm.core.vector_store", flowllm_vector_store_pkg)
    monkeypatch.setitem(
        sys.modules,
        "flowllm.core.vector_store.qdrant_vector_store",
        qdrant_vs_mod,
    )
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_pkg)
    monkeypatch.setitem(sys.modules, "qdrant_client.http", qdrant_http_pkg)
    monkeypatch.setitem(sys.modules, "qdrant_client.http.models", qdrant_models_mod)

    settings = LTMSettings(
        enabled=True,
        control_mode="both",
        attach_scope="orchestrator",
        memories=("task", "tool"),
        fail_open=False,
        retrieve_limit=5,
        write_gate_enabled=True,
        static_record_enabled=False,
        model_name="qwen3.5-35b-a3b",
        embedding_model_name="text-embedding-v4",
        rerank_model_name="qwen3-vl-rerank",
        global_workspace="global_task_tool_v1",
        api_key="dummy",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_dimensions=1024,
        reme_config_path="/tmp/reme_qdrant_shared.yaml",
        reme_kwargs={},
    )

    _prepare_qdrant_vector_store(settings)

    assert _DummyQdrantVectorStore.captured_types_namespace is not None
    assert (
        _DummyQdrantVectorStore.captured_types_namespace.get("Distance")
        is _DummyDistance
    )


def test_patch_async_qdrant_search_compat_maps_to_query_points(monkeypatch):
    class _DummyQueryResponse:
        def __init__(self, points):
            self.points = points

    class _DummyAsyncQdrantClient:
        def __init__(self):
            self.calls = []

        async def query_points(
            self,
            *,
            collection_name,
            query,
            limit,
            query_filter,
            with_payload,
            with_vectors,
            **kwargs,
        ):
            self.calls.append(
                {
                    "collection_name": collection_name,
                    "query": query,
                    "limit": limit,
                    "query_filter": query_filter,
                    "with_payload": with_payload,
                    "with_vectors": with_vectors,
                    "kwargs": kwargs,
                },
            )
            return _DummyQueryResponse(points=[{"id": "pt-1"}])

    qdrant_pkg = types.ModuleType("qdrant_client")
    qdrant_pkg.AsyncQdrantClient = _DummyAsyncQdrantClient
    monkeypatch.setitem(sys.modules, "qdrant_client", qdrant_pkg)

    settings = LTMSettings(
        enabled=True,
        control_mode="both",
        attach_scope="orchestrator",
        memories=("task", "tool"),
        fail_open=False,
        retrieve_limit=5,
        write_gate_enabled=True,
        static_record_enabled=False,
        model_name="qwen3.5-35b-a3b",
        embedding_model_name="text-embedding-v4",
        rerank_model_name="qwen3-vl-rerank",
        global_workspace="global_task_tool_v1",
        api_key="dummy",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_dimensions=1024,
        reme_config_path="/tmp/reme_qdrant_shared.yaml",
        reme_kwargs={},
    )

    _patch_async_qdrant_search_compat(settings)

    assert hasattr(_DummyAsyncQdrantClient, "search")
    client = _DummyAsyncQdrantClient()
    points = asyncio.run(
        client.search(
            collection_name="demo",
            query_vector=[0.1, 0.2],
            limit=3,
            query_filter={"must": []},
            with_payload=True,
            with_vectors=False,
            score_threshold=0.6,
        ),
    )

    assert points == [{"id": "pt-1"}]
    assert client.calls == [
        {
            "collection_name": "demo",
            "query": [0.1, 0.2],
            "limit": 3,
            "query_filter": {"must": []},
            "with_payload": True,
            "with_vectors": False,
            "kwargs": {"score_threshold": 0.6},
        },
    ]


def test_sanitize_qdrant_distance_override_keeps_kwargs_when_absent():
    settings = LTMSettings(
        enabled=True,
        control_mode="both",
        attach_scope="orchestrator",
        memories=("task", "tool"),
        fail_open=True,
        retrieve_limit=5,
        write_gate_enabled=True,
        static_record_enabled=False,
        model_name="qwen3.5-35b-a3b",
        embedding_model_name="text-embedding-v4",
        rerank_model_name="qwen3-vl-rerank",
        global_workspace="global_task_tool_v1",
        api_key="dummy",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_dimensions=1024,
        reme_config_path="/tmp/reme_qdrant_shared.yaml",
        reme_kwargs={},
    )

    normalized = _sanitize_qdrant_distance_override(settings)

    assert "vector_store.default.params.distance" not in normalized
    assert settings.reme_kwargs == {}


def test_sanitize_qdrant_distance_override_removes_explicit_value():
    settings = LTMSettings(
        enabled=True,
        control_mode="both",
        attach_scope="orchestrator",
        memories=("task", "tool"),
        fail_open=True,
        retrieve_limit=5,
        write_gate_enabled=True,
        static_record_enabled=False,
        model_name="qwen3.5-35b-a3b",
        embedding_model_name="text-embedding-v4",
        rerank_model_name="qwen3-vl-rerank",
        global_workspace="global_task_tool_v1",
        api_key="dummy",
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1",
        embedding_dimensions=1024,
        reme_config_path="/tmp/reme_qdrant_shared.yaml",
        reme_kwargs={"vector_store.default.params.distance": "COSINE"},
    )

    normalized = _sanitize_qdrant_distance_override(settings)

    assert "vector_store.default.params.distance" not in normalized
    assert settings.reme_kwargs["vector_store.default.params.distance"] == "COSINE"
