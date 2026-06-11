"""OpenRouter-compatible formatter variants for reasoning continuation.

OpenRouter reasoning models can continue multi-step tool use more reliably when
the prior assistant message includes the provider-issued `reasoning_details`
payload unchanged. AgentScope's default OpenAI formatter drops both
ThinkingBlock and message metadata, so this formatter preserves the
provider-specific reasoning payload when it is present in `Msg.metadata`.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from agentscope._logging import logger
from agentscope.formatter import OpenAIChatFormatter
from agentscope.formatter._openai_formatter import (
    _format_openai_image_block,
    _to_openai_audio_data,
)
from agentscope.message import ImageBlock, Msg, TextBlock, ThinkingBlock, URLSource


OPENROUTER_REASONING_DETAILS_KEY = "_openrouter_reasoning_details"


class OpenRouterReasoningChatFormatter(OpenAIChatFormatter):
    """OpenAI formatter variant that preserves OpenRouter reasoning_details."""

    def __init__(self, *, replay_reasoning_content: bool = False) -> None:
        super().__init__()
        self._replay_reasoning_content = replay_reasoning_content

    supported_blocks = [
        *OpenAIChatFormatter.supported_blocks,
        ThinkingBlock,
    ]

    @staticmethod
    def _reasoning_details_from_msg(msg: Msg) -> Any | None:
        metadata = getattr(msg, "metadata", None)
        if not isinstance(metadata, dict):
            return None
        return metadata.get(OPENROUTER_REASONING_DETAILS_KEY)

    async def _format(self, msgs: list[Msg]) -> list[dict[str, Any]]:
        """Format AgentScope messages into OpenRouter OpenAI-compatible payloads."""
        self.assert_list_of_msgs(msgs)

        messages: list[dict[str, Any]] = []
        i = 0
        while i < len(msgs):
            msg = msgs[i]
            content_blocks: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []
            reasoning_details = self._reasoning_details_from_msg(msg)
            reasoning_chunks: list[str] = []

            for block in msg.get_content_blocks():
                typ = block.get("type")
                if typ == "text":
                    content_blocks.append({**block})

                elif typ == "thinking":
                    if self._replay_reasoning_content:
                        thinking_text = str(block.get("thinking") or "")
                        if thinking_text:
                            reasoning_chunks.append(thinking_text)
                    # Base OpenRouter continuation replays reasoning_details.
                    # The Kimi-specific hybrid formatter additionally replays
                    # plaintext reasoning_content as an experiment.
                    continue

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
                    textual_output, multimodal_data = self.convert_tool_result_to_string(
                        block["output"],
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
                    content_blocks.append(_format_openai_image_block(block))

                elif typ == "audio":
                    input_audio = _to_openai_audio_data(block["source"])
                    content_blocks.append(
                        {
                            "type": "input_audio",
                            "input_audio": input_audio,
                        },
                    )

                else:
                    logger.warning(
                        "Unsupported block type %s in the message, skipped.",
                        typ,
                    )

            msg_openai: dict[str, Any] = {
                "role": msg.role,
                "name": msg.name,
                "content": content_blocks or None,
            }

            if tool_calls:
                msg_openai["tool_calls"] = tool_calls

            if reasoning_details is not None:
                msg_openai["reasoning_details"] = deepcopy(reasoning_details)
            if reasoning_chunks:
                msg_openai["reasoning_content"] = "\n".join(reasoning_chunks)

            if (
                msg_openai["content"]
                or msg_openai.get("tool_calls")
                or msg_openai.get("reasoning_content")
                or msg_openai.get("reasoning_details") is not None
            ):
                messages.append(msg_openai)

            i += 1

        return messages


class OpenRouterKimiHybridFormatter(OpenRouterReasoningChatFormatter):
    """OpenRouter formatter for Kimi that replays both reasoning payload styles."""

    def __init__(self) -> None:
        super().__init__(replay_reasoning_content=True)
