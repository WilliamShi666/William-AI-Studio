"""
Quick test script to inspect Msg.get_content_blocks() for ReActAgent.

Run this file directly (inside your virtualenv with DASHSCOPE_API_KEY set):

    cd backend/multiagent_debaters/multiagent_debaters
    python block_test.py
"""

import asyncio
import os

from dotenv import load_dotenv

from agentscope.agent import ReActAgent
from agentscope.formatter import DashScopeChatFormatter
from agentscope.message import Msg
from agentscope.model import DashScopeChatModel


def debug_pre_print(self, kwargs):
    """Print block types and content for debugging."""
    msg = kwargs.get("msg")
    if msg is None:
        print("DEBUG HOOK: no msg in kwargs")
        return kwargs

    try:
        blocks = msg.get_content_blocks()
    except Exception as exc:  # noqa: BLE001
        print("DEBUG HOOK: get_content_blocks failed:", exc)
        print("DEBUG HOOK: raw msg.content =", getattr(msg, "content", None))
        return kwargs

    print(f"DEBUG HOOK: got {len(blocks)} block(s)")
    for idx, block in enumerate(blocks):
        btype = getattr(block, "type", None) or block.get("type", None)  # type: ignore[attr-defined]
        print(f"  Block {idx}: type={btype} | data={block}")

    return kwargs


async def main() -> None:
    load_dotenv()
    api_key = os.getenv("DASHSCOPE_API_KEY")
    if not api_key:
        print("ERROR: DASHSCOPE_API_KEY not set in environment")
        return

    # Register class-level hook so all ReActAgent instances will trigger it.
    ReActAgent.register_class_hook(
        "pre_print",
        "debug_pre_print_hook",
        debug_pre_print,
    )

    model = DashScopeChatModel(
        model_name="qwen3-max",
        api_key=api_key,
        stream=True,  # use streaming mode to see incremental content
    )

    agent = ReActAgent(
        name="DebugAgent",
        sys_prompt="你是一名辩手，请简要说明你支持或反对该辩题。",
        model=model,
        formatter=DashScopeChatFormatter(),
    )

    print("=== Sending test message to ReActAgent (stream=True) ===")
    msg = await agent(
        Msg(
            "user",
            "辩题：本院相信人类拥有自由意志。请给出你的立场和简要论证。",
            "user",
        ),
    )
    print("\n=== Final Msg.content ===")
    print(getattr(msg, "content", None))


if __name__ == "__main__":
    asyncio.run(main())
