"""Project-local ReActAgent variant that preserves ChatResponse metadata."""

from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncGenerator, Literal, Type

from agentscope.agent import ReActAgent
from agentscope.agent._react_agent import _AsyncNullContext, _MemoryMark
from agentscope.message import AudioBlock, Msg, ToolResultBlock, ToolUseBlock
from agentscope.tracing import trace_reply
from pydantic import BaseModel
from utils.logger import logger


class MetadataAwareReActAgent(ReActAgent):
    """ReActAgent that carries model response metadata into assistant memory."""

    @staticmethod
    def _read_positive_int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        if raw is None:
            return default
        try:
            value = int(str(raw).strip())
        except ValueError:
            return default
        return value if value > 0 else default

    @classmethod
    def _thinking_only_required_tool_threshold(cls) -> int:
        return cls._read_positive_int_env(
            "AGENTSCOPE_THINKING_ONLY_REQUIRED_TOOL_THRESHOLD",
            8,
        )

    @classmethod
    def _thinking_only_text_response_threshold(cls) -> int:
        return cls._read_positive_int_env(
            "AGENTSCOPE_THINKING_ONLY_TEXT_RESPONSE_THRESHOLD",
            14,
        )

    @classmethod
    def _thinking_only_exit_threshold(cls) -> int:
        return cls._read_positive_int_env(
            "AGENTSCOPE_THINKING_ONLY_EXIT_THRESHOLD",
            18,
        )

    @staticmethod
    def _build_tool_result_msg(
        *,
        tool_call_id: str,
        tool_name: str,
        output: Any,
    ) -> Msg:
        """Build a tool_result message with user-role salience.

        AgentScope upstream records tool results as ``system`` messages.  For
        OpenAI-compatible reasoning models such as DeepSeek, replaying tool
        observations with ``user`` role makes the observation part of the
        normal action/observation dialogue instead of low-salience environment
        metadata.  The formatter still serializes ToolResultBlock as
        ``role=tool`` for the provider API.
        """
        return Msg(
            "tool",
            [
                ToolResultBlock(
                    type="tool_result",
                    id=tool_call_id,
                    name=tool_name,
                    output=output,
                ),
            ],
            "user",
        )

    @staticmethod
    def _apply_response_metadata(msg: Msg, response: Any) -> None:
        metadata = getattr(response, "metadata", None)
        if isinstance(metadata, dict) and metadata:
            msg.metadata = dict(metadata)

    @staticmethod
    def _has_thinking_blocks(msg: Msg) -> bool:
        """Check whether *msg* contains thinking/reasoning blocks.

        Upstream ``has_content_blocks`` does not accept ``"thinking"`` in its
        ``Literal`` type, so we call ``get_content_blocks`` directly.
        """
        return len(msg.get_content_blocks("thinking")) > 0

    async def _reasoning(
        self,
        tool_choice: Literal["auto", "none", "required"] | None = None,
    ) -> Msg:
        """Perform the reasoning process while preserving response metadata."""
        if self.plan_notebook:
            hint_msg = await self.plan_notebook.get_current_hint()
            if self.print_hint_msg and hint_msg:
                await self.print(hint_msg)
            await self.memory.add(hint_msg, marks=_MemoryMark.HINT)

        prompt = await self.formatter.format(
            msgs=[
                Msg("system", self.sys_prompt, "system"),
                *await self.memory.get_memory(),
            ],
        )
        await self.memory.delete_by_mark(mark=_MemoryMark.HINT)

        res = await self.model(
            prompt,
            tools=self.toolkit.get_json_schemas(),
            tool_choice=tool_choice,
        )

        interrupted_by_user = False
        msg = None

        tts_context = self.tts_model or _AsyncNullContext()
        speech: AudioBlock | list[AudioBlock] | None = None

        try:
            async with tts_context:
                msg = Msg(name=self.name, content=[], role="assistant")
                if self.model.stream:
                    async for content_chunk in res:
                        msg.content = content_chunk.content
                        self._apply_response_metadata(msg, content_chunk)

                        speech = msg.get_content_blocks("audio") or None

                        if (
                            self.tts_model
                            and self.tts_model.supports_streaming_input
                        ):
                            tts_res = await self.tts_model.push(msg)
                            speech = tts_res.content

                        await self.print(msg, False, speech=speech)

                else:
                    msg.content = list(res.content)
                    self._apply_response_metadata(msg, res)

                if self.tts_model:
                    tts_res = await self.tts_model.synthesize(msg)
                    if self.tts_model.stream:
                        async for tts_chunk in tts_res:
                            speech = tts_chunk.content
                            await self.print(msg, False, speech=speech)
                    else:
                        speech = tts_res.content

                await self.print(msg, True, speech=speech)
                await asyncio.sleep(0.001)

        except asyncio.CancelledError as error:
            interrupted_by_user = True
            raise error from None

        finally:
            await self.memory.add(msg)

            if interrupted_by_user and msg:
                tool_use_blocks: list = msg.get_content_blocks("tool_use")
                for tool_call in tool_use_blocks:
                    msg_res = self._build_tool_result_msg(
                        tool_call_id=tool_call["id"],
                        tool_name=tool_call["name"],
                        output="The tool call has been interrupted by the user.",
                    )
                    await self.memory.add(msg_res)
                    await self.print(msg_res, True)
        return msg

    async def _acting(self, tool_call: ToolUseBlock) -> dict | None:
        """Execute a tool call and record the result as a user-role observation."""
        tool_res_msg = self._build_tool_result_msg(
            tool_call_id=tool_call["id"],
            tool_name=tool_call["name"],
            output=[],
        )
        try:
            tool_res = await self.toolkit.call_tool_function(tool_call)

            async for chunk in tool_res:
                tool_res_msg.content[0]["output"] = chunk.content  # type: ignore[index]

                await self.print(tool_res_msg, chunk.is_last)

                if chunk.is_interrupted:
                    raise asyncio.CancelledError()

                if (
                    tool_call["name"] == self.finish_function_name
                    and chunk.metadata
                    and chunk.metadata.get("success", False)
                ):
                    return chunk.metadata.get("structured_output")

            return None

        finally:
            await self.memory.add(tool_res_msg)

    async def _summarizing(self) -> Msg:
        """Generate a fallback summary while preserving response metadata."""
        hint_msg = Msg(
            "user",
            "You have failed to generate response within the maximum "
            "iterations. Now respond directly by summarizing the current "
            "situation.",
            role="user",
        )

        prompt = await self.formatter.format(
            [
                Msg("system", self.sys_prompt, "system"),
                *await self.memory.get_memory(),
                hint_msg,
            ],
        )
        res = await self.model(prompt)

        tts_context = self.tts_model or _AsyncNullContext()
        speech: AudioBlock | list[AudioBlock] | None = None

        async with tts_context:
            res_msg = Msg(self.name, [], "assistant")
            if isinstance(res, AsyncGenerator):
                async for chunk in res:
                    res_msg.content = chunk.content
                    self._apply_response_metadata(res_msg, chunk)

                    speech = res_msg.get_content_blocks("audio") or None

                    if (
                        self.tts_model
                        and self.tts_model.supports_streaming_input
                    ):
                        tts_res = await self.tts_model.push(res_msg)
                        speech = tts_res.content

                    await self.print(res_msg, False, speech=speech)

            else:
                res_msg.content = res.content
                self._apply_response_metadata(res_msg, res)

            if self.tts_model:
                tts_res = await self.tts_model.synthesize(res_msg)
                if self.tts_model.stream:
                    async for tts_chunk in tts_res:
                        speech = tts_chunk.content
                        await self.print(res_msg, False, speech=speech)
                else:
                    speech = tts_res.content

            await self.print(res_msg, True, speech=speech)
            return res_msg

    # ── reply() override with positive completion check ─────────────────

    @trace_reply
    async def reply(  # pylint: disable=too-many-branches
        self,
        msg: Msg | list[Msg] | None = None,
        structured_model: Type[BaseModel] | None = None,
    ) -> Msg:
        """Generate a reply based on the current state and input arguments.

        Overrides upstream to use a **positive** completion check:
        the agent only exits the loop when the model returns text content
        without tool calls.  Thinking-only or empty responses are treated
        as "still processing" and the loop continues.
        """
        await self.memory.add(msg)

        await self._retrieve_from_long_term_memory(msg)
        await self._retrieve_from_knowledge(msg)

        tool_choice: Literal["auto", "none", "required"] | None = None

        self._required_structured_model = structured_model
        if structured_model:
            if self.finish_function_name not in self.toolkit.tools:
                self.toolkit.register_tool_function(
                    getattr(self, self.finish_function_name),
                )
            self.toolkit.set_extended_model(
                self.finish_function_name,
                structured_model,
            )
            tool_choice = "required"
        else:
            self.toolkit.remove_tool_function(self.finish_function_name)

        structured_output = None
        reply_msg = None
        self._consecutive_thinking_count = 0
        for _ in range(self.max_iters):
            await self._compress_memory_if_needed()

            msg_reasoning = await self._reasoning(tool_choice)

            futures = [
                self._acting(tool_call)
                for tool_call in msg_reasoning.get_content_blocks("tool_use")
            ]
            if self.parallel_tool_calls:
                structured_outputs = await asyncio.gather(*futures)
            else:
                structured_outputs = [await _ for _ in futures]

            if self._required_structured_model:
                structured_outputs = [_ for _ in structured_outputs if _]

                msg_hint = None
                if structured_outputs:
                    structured_output = structured_outputs[-1]

                    if msg_reasoning.has_content_blocks("text"):
                        reply_msg = Msg(
                            self.name,
                            msg_reasoning.get_content_blocks("text"),
                            "assistant",
                            metadata=structured_output,
                        )
                        break

                    msg_hint = Msg(
                        "user",
                        "<system-hint>Now generate a text "
                        "response based on your current situation"
                        "</system-hint>",
                        "user",
                    )
                    await self.memory.add(msg_hint, marks=_MemoryMark.HINT)

                    tool_choice = "none"
                    self._required_structured_model = None

                elif not msg_reasoning.has_content_blocks("tool_use"):
                    msg_hint = Msg(
                        "user",
                        "<system-hint>Structured output is "
                        f"required, go on to finish your task or call "
                        f"'{self.finish_function_name}' to generate the "
                        f"required structured output.</system-hint>",
                        "user",
                    )
                    await self.memory.add(msg_hint, marks=_MemoryMark.HINT)
                    tool_choice = "required"

                if msg_hint and self.print_hint_msg:
                    await self.print(msg_hint)

            # ── CHANGED: positive completion check with escalation ──────
            elif not msg_reasoning.has_content_blocks("tool_use"):
                # Only exit when the model produced actual text content.
                if msg_reasoning.has_content_blocks("text"):
                    msg_reasoning.metadata = structured_output
                    reply_msg = msg_reasoning
                    break

                # thinking-only or empty → inject hint, don't exit
                self._consecutive_thinking_count += 1

                # ── Progressive escalation ──────────────────────────────
                count = self._consecutive_thinking_count
                exit_threshold = self._thinking_only_exit_threshold()
                # Preserve the anti-runaway guarantee even when deployments use
                # a higher DeepSeek-friendly default.  The extra summarizing
                # call consumes one model turn, so break before max_iters.
                exit_threshold = min(
                    exit_threshold,
                    max(10, self.max_iters - 2),
                )
                required_threshold = self._thinking_only_required_tool_threshold()
                text_response_threshold = self._thinking_only_text_response_threshold()

                if count >= exit_threshold:
                    # Stuck — exit early, let _summarizing handle it
                    logger.warning(
                        "[MetadataAwareReActAgent] Agent %s stuck in "
                        "thinking-only loop after %s consecutive rounds; "
                        "exiting early.",
                        self.name,
                        count,
                    )
                    break

                if count < required_threshold:
                    hint_text = (
                        "<system-hint>You were still reasoning. "
                        "Continue with your next action — call a tool "
                        "if needed to make progress.</system-hint>"
                    )
                    # tool_choice stays as-is (auto)
                elif count < text_response_threshold:
                    hint_text = (
                        "<system-hint>You have been reasoning for "
                        "several rounds without taking action. "
                        "You MUST call a tool NOW to make progress. "
                        "Choose the most appropriate tool and use it."
                        "</system-hint>"
                    )
                    tool_choice = "required"
                else:
                    hint_text = (
                        "<system-hint>CRITICAL: You have not taken any "
                        "action for many rounds. Stop reasoning and "
                        "produce a text response explaining what you "
                        "have done so far and what remains to be done. "
                        "Do NOT call any tools — just write text."
                        "</system-hint>"
                    )
                    tool_choice = "none"

                hint_msg = Msg("user", hint_text, "user")
                await self.memory.add(hint_msg, marks=_MemoryMark.HINT)
                if self.print_hint_msg:
                    await self.print(hint_msg)
            else:
                # Model produced tool_use block(s) → reset the counter
                self._consecutive_thinking_count = 0
            # ───────────────────────────────────────────────────────────

        if reply_msg is None:
            reply_msg = await self._summarizing()
            reply_msg.metadata = structured_output
            await self.memory.add(reply_msg)

        if self._static_control:
            await self.long_term_memory.record(
                [
                    *([*msg] if isinstance(msg, list) else [msg]),
                    *await self.memory.get_memory(
                        exclude_mark=_MemoryMark.COMPRESSED,
                    ),
                    reply_msg,
                ],
            )

        return reply_msg
