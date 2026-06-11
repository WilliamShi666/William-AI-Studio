# -*- coding: utf-8 -*-
"""Unit tests for the multi-agent debate workflow example."""
from unittest import IsolatedAsyncioTestCase

from pydantic import BaseModel, Field

from agentscope.agent import ReActAgent
from agentscope.formatter import (
    DashScopeChatFormatter,
    DashScopeMultiAgentFormatter,
)
from agentscope.message import Msg, TextBlock, ToolUseBlock
from agentscope.model import ChatModelBase, ChatResponse

from examples.workflows.multiagent_debate.main import (
    TOTAL_ROUNDS,
    ROUND_LABELS,
    build_debater_prompt,
    JudgeModel,
    run_multiagent_debate,
)


class DebaterTestModel(ChatModelBase):
    """Test model for debater agents that records prompts."""

    def __init__(self) -> None:
        """Initialize the test model."""
        super().__init__("debater_test_model", stream=False)
        self.user_prompts: list[str] = []

    async def __call__(self, messages: list[dict], **_) -> ChatResponse:
        """Record the last user prompt and return a dummy response."""
        last_user = None
        for m in messages:
            if m["role"] == "user":
                last_user = m["content"]
        if isinstance(last_user, str):
            self.user_prompts.append(last_user)

        return ChatResponse(
            content=[
                TextBlock(
                    type="text",
                    text="test response",
                ),
            ],
        )


class ModeratorTestModel(ChatModelBase):
    """Test model for the moderator using structured output."""

    def __init__(self) -> None:
        """Initialize the test model."""
        super().__init__("moderator_test_model", stream=False)
        self.call_cnt = 0

    async def __call__(self, _messages: list[dict], **_) -> ChatResponse:
        """Return a tool call for JudgeModel."""
        self.call_cnt += 1
        # Finish after the last round, otherwise continue
        finished = self.call_cnt >= TOTAL_ROUNDS
        tool_input = {
            "finished": finished,
            "correct_answer": "42" if finished else None,
        }
        return ChatResponse(
            content=[
                ToolUseBlock(
                    type="tool_use",
                    name="generate_response",
                    id=str(self.call_cnt),
                    input=tool_input,
                ),
            ],
        )


class DummyJudgeModel(BaseModel):
    """Dummy JudgeModel for testing explicit prompt content."""

    finished: bool = Field(description="Whether the debate is finished.")
    correct_answer: str | None = Field(
        description="The correct answer.",
        default=None,
    )


class MultiAgentDebateTest(IsolatedAsyncioTestCase):
    """Tests for the three-round multi-agent debate workflow."""

    async def test_round_constants(self) -> None:
        """Ensure we have exactly three rounds with expected labels."""
        self.assertEqual(TOTAL_ROUNDS, 3)
        self.assertEqual(
            ROUND_LABELS[1],
            "Opening / Main Case",
        )
        self.assertEqual(
            ROUND_LABELS[2],
            "Rebuttals & Additional Arguments",
        )
        self.assertIn("No New Arguments", ROUND_LABELS[3])

    async def test_build_debater_prompt_content(self) -> None:
        """Check that prompts contain round-specific instructions."""
        topic = "Test motion"

        p1_aff = build_debater_prompt("affirmative", 1, topic)
        self.assertIn("Round 1 – Opening / Main Case", p1_aff)
        self.assertIn("Define the issue / motion clearly", p1_aff)
        self.assertIn("standard / burden for winning", p1_aff)

        p1_opp = build_debater_prompt("opposition", 1, topic)
        self.assertIn("Briefly respond to the Affirmative's setup", p1_opp)
        self.assertIn("main Opposition case", p1_opp)

        p2_aff = build_debater_prompt("affirmative", 2, topic)
        self.assertIn("Rebuttals to Opposition", p2_aff)
        self.assertIn("Additional Arguments for the Affirmative", p2_aff)

        p3_aff = build_debater_prompt("affirmative", 3, topic)
        self.assertIn("no new arguments are allowed", p3_aff.lower())
        self.assertIn(
            "Weigh and compare the existing arguments".lower(),
            p3_aff.lower(),
        )

    async def test_three_round_sequence(self) -> None:
        """Run the debate with dummy models and verify three rounds."""
        # Debater agents with recording models
        aff_model = DebaterTestModel()
        opp_model = DebaterTestModel()
        mod_model = ModeratorTestModel()

        affirmative = ReActAgent(
            name="Affirmative",
            sys_prompt="Affirmative test agent.",
            model=aff_model,
            formatter=DashScopeChatFormatter(),
        )
        opposition = ReActAgent(
            name="Opposition",
            sys_prompt="Opposition test agent.",
            model=opp_model,
            formatter=DashScopeChatFormatter(),
        )
        moderator = ReActAgent(
            name="Aggregator",
            sys_prompt="Moderator test agent.",
            model=mod_model,
            formatter=DashScopeMultiAgentFormatter(),
        )

        await run_multiagent_debate(
            topic="Test motion",
            max_rounds=TOTAL_ROUNDS,
            affirmative_agent=affirmative,
            opposition_agent=opposition,
            moderator_agent=moderator,
        )

        # Each debater should have spoken exactly once per round
        self.assertEqual(len(aff_model.user_prompts), TOTAL_ROUNDS)
        self.assertEqual(len(opp_model.user_prompts), TOTAL_ROUNDS)

        # Prompts should progress through the three rounds in order
        self.assertIn("Round 1 – Opening / Main Case", aff_model.user_prompts[0])
        self.assertIn(
            "Round 2 – Rebuttals & Additional Arguments",
            aff_model.user_prompts[1],
        )
        self.assertIn("Round 3 – Evaluation / Weighing", aff_model.user_prompts[2])

        # Moderator should have been called once per round and finish at the end
        self.assertEqual(mod_model.call_cnt, TOTAL_ROUNDS)

        # Sanity check that JudgeModel still exists and is compatible
        _ = JudgeModel(finished=True, correct_answer="ok")
        _ = DummyJudgeModel(finished=False)

