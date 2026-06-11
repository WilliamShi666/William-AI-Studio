"""Moonshot-compatible formatter for AgentScope OpenAI chat payloads.

Moonshot Kimi thinking mode requires preserving reasoning_content across
assistant tool-call rounds. AgentScope's default OpenAI formatter currently
skips ThinkingBlock, so this formatter extends it by serializing thinking
blocks to the top-level `reasoning_content` field.
"""

from __future__ import annotations

import json
from typing import Any

from agentscope.formatter import OpenAIChatFormatter
from agentscope.formatter._openai_formatter import (
    _format_openai_image_block,
    _to_openai_audio_data,
)
from agentscope.message import Msg, ThinkingBlock, TextBlock, ImageBlock, URLSource
from agentscope._logging import logger


class MoonshotChatFormatter(OpenAIChatFormatter):
    """OpenAI formatter variant that preserves ThinkingBlock as reasoning_content."""

    supported_blocks = [
        *OpenAIChatFormatter.supported_blocks,
        ThinkingBlock,
    ]

    async def _format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        """Format AgentScope messages into Moonshot OpenAI-compatible payloads."""
        self.assert_list_of_msgs(msgs)

        messages: list[dict[str, Any]] = []
        stripped_reasoning = 0
        preserved_reasoning = 0
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            content_blocks: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []
            reasoning_chunks: list[str] = []

            for block in msg.get_content_blocks():
                typ = block.get("type")
                if typ == "text":
                    content_blocks.append({**block})

                elif typ == "thinking":
                    thinking_text = str(block.get("thinking") or "")
                    if thinking_text:
                        reasoning_chunks.append(thinking_text)

                elif typ == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(
                                    block.get("input", {}),
                                    ensure_ascii=False,
                                ),
                            },
                        },
                    )

                elif typ == "tool_result":
                    textual_output, multimodal_data = (
                        self.convert_tool_result_to_string(
                            block["output"],
                        )
                    )

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": block.get("id"),
                            "content": textual_output,
                            "name": block.get("name"),
                        },
                    )

                    promoted_blocks: list = []
                    for url, multimodal_block in multimodal_data:
                        if (
                            multimodal_block["type"] == "image"
                            and self.promote_tool_result_images
                        ):
                            promoted_blocks.extend(
                                [
                                    TextBlock(
                                        type="text",
                                        text=f"\n- The image from '{url}': ",
                                    ),
                                    ImageBlock(
                                        type="image",
                                        source=URLSource(type="url", url=url),
                                    ),
                                ],
                            )

                    if promoted_blocks:
                        promoted_blocks = [
                            TextBlock(
                                type="text",
                                text="<system-info>The following are "
                                "the image contents from the tool "
                                f"result of '{block['name']}':",
                            ),
                            *promoted_blocks,
                            TextBlock(type="text", text="</system-info>"),
                        ]
                        msgs.insert(
                            i + 1,
                            Msg(
                                name="user",
                                content=promoted_blocks,
                                role="user",
                            ),
                        )

                elif typ == "image":
                    content_blocks.append(
                        _format_openai_image_block(block),
                    )

                elif typ == "audio":
                    input_audio = _to_openai_audio_data(block["source"])
                    content_blocks.append(
                        {
                            "type": "input_audio",
                            "input_audio": input_audio,
                        },
                    )

                else:
                    # Keep the same unsupported-block behavior as base formatter.
                    logger.warning(
                        "Unsupported block type %s in the message, skipped.",
                        typ,
                    )

            content_value: list[dict[str, Any]] | str | None
            if content_blocks:
                content_value = content_blocks
            elif tool_calls:
                content_value = ""
            else:
                content_value = None

            msg_openai: dict[str, Any] = {
                "role": msg.role,
                "name": msg.name,
                "content": content_value,
            }

            if msg.role == "assistant" and tool_calls and not reasoning_chunks:
                reasoning_chunks.append("Proceeding with the requested tool call.")

            if reasoning_chunks:
                msg_openai["reasoning_content"] = "\n".join(reasoning_chunks)

            if tool_calls:
                msg_openai["tool_calls"] = tool_calls

            # Per DeepSeek docs: reasoning_content from non-tool-call turns is
            # ignored by the API. Strip it to save context (can be 50-70% of
            # total prompt tokens in long conversations).
            if msg.role == "assistant" and not tool_calls:
                if msg_openai.pop("reasoning_content", None):
                    stripped_reasoning += 1
            elif msg.role == "assistant" and msg_openai.get("reasoning_content"):
                preserved_reasoning += 1

            if (
                msg_openai["content"]
                or msg_openai.get("tool_calls")
                or msg_openai.get("reasoning_content")
            ):
                messages.append(msg_openai)

            i += 1

        if stripped_reasoning or preserved_reasoning:
            logger.info(
                "[MoonshotFormatter] reasoning: stripped=%d preserved=%d (total_msgs=%d)",
                stripped_reasoning,
                preserved_reasoning,
                len(messages),
            )
        return messages
