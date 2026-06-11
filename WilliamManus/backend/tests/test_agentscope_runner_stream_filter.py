import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agentscope_integration.runner import _is_internal_user_echo_sse


def test_internal_user_echo_sse_is_suppressed_but_assistant_and_tool_are_not() -> None:
    assert _is_internal_user_echo_sse(
        {"type": "user", "is_llm_message": False, "content": '{"role":"user"}'}
    )
    assert not _is_internal_user_echo_sse(
        {"type": "assistant", "is_llm_message": True}
    )
    assert not _is_internal_user_echo_sse(
        {"type": "tool", "is_llm_message": False}
    )
