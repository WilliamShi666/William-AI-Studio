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

from agentscope_integration.memory.long_term.ltm_policy import (
    bucketize_memory_content,
    sanitize_keywords,
)


def test_bucketize_splits_task_and_tool_and_applies_dedup_and_sensitive_filter() -> None:
    tool_payload = json.dumps(
        {
            "create_time": "2026-02-28 10:00:00",
            "tool_name": "browser_navigate_to",
            "input": {"url": "https://example.com"},
            "output": "Navigation succeeded and page loaded.",
            "token_cost": 42,
            "success": True,
            "time_cost": 1.8,
        },
        ensure_ascii=False,
    )

    buckets = bucketize_memory_content(
        [
            "Use explicit retry budget for flaky sandbox commands and log terminal stderr patterns.",
            "Use explicit retry budget for flaky sandbox commands and log terminal stderr patterns.",
            tool_payload,
            "my api_key is sk-1234567890abcdef",
            "short",
        ],
        write_gate_enabled=True,
    )

    assert len(buckets.task_items) == 1
    assert len(buckets.tool_items) == 1
    assert buckets.dropped_count >= 2
    assert buckets.drop_reasons.get("duplicate", 0) >= 1
    assert buckets.drop_reasons.get("sensitive", 0) >= 1


def test_sanitize_keywords_filters_sensitive_and_empty_entries() -> None:
    keywords = sanitize_keywords(
        ["", "tool retry", "   ", "password reset token", "sandbox resume"],
        write_gate_enabled=True,
    )

    assert "tool retry" in keywords
    assert "sandbox resume" in keywords
    assert all("password" not in item.lower() for item in keywords)
