# -*- coding: utf-8 -*-
"""The multi-agent debate workflow example in AgentScope."""
import asyncio
import os
from dotenv import load_dotenv

from pydantic import (
    BaseModel,
    Field,
)

from agentscope.agent import ReActAgent
from agentscope.formatter import (
    DashScopeChatFormatter,
    DashScopeMultiAgentFormatter,
)
from agentscope.message import Msg
from agentscope.model import DashScopeChatModel, OpenAIChatModel
from agentscope.pipeline import MsgHub

load_dotenv()

DEFAULT_TOPIC = (
    "The two circles are externally tangent and there is no relative sliding. "
    "The radius of circle A is 1/3 the radius of circle B. Circle A rolls "
    "around circle B one trip back to its starting point. How many times will "
    "circle A revolve in total?"
)


TOTAL_ROUNDS = 3
ROUND_LABELS = {
    1: "Opening / Main Case",
    2: "Rebuttals & Additional Arguments",
    3: "Evaluation / Weighing (No New Arguments)",
}

# Provider configuration
DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

DEFAULT_DEBATE_MODEL = "deepseek-v4-flash"


def _get_provider_for_model(model_name: str) -> str:
    """Determine provider from model name prefix."""
    if model_name.startswith("deepseek-"):
        return "deepseek"
    return "dashscope"  # default for qwen*, kimi* models


def resolve_dashscope_key() -> str:
    """优先读取专用配置，缺失时回退到通用模型密钥。"""
    return os.environ.get("DASHSCOPE_API_KEY") or os.environ.get("API_KEY") or ""


def resolve_api_key(provider: str) -> str:
    """Resolve API key based on provider."""
    if provider == "deepseek":
        return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("API_KEY") or ""
    return resolve_dashscope_key()


def _create_chat_model(model_name: str):
    """Create the appropriate AgentScope chat model based on model name prefix."""
    provider = _get_provider_for_model(model_name)
    api_key = resolve_api_key(provider)

    if provider == "deepseek":
        return OpenAIChatModel(
            model_name=model_name,
            api_key=api_key,
            stream=True,
            client_args={"base_url": DEEPSEEK_BASE_URL},
        )
    else:
        # dashscope: use DashScopeChatModel for native support
        return DashScopeChatModel(
            model_name=model_name,
            api_key=api_key,
            stream=True,
        )


# Create two debater agents, Alice and Bob, who will discuss the topic.
def create_solver_agent(
    name: str,
    topic: str,
    model_name: str = DEFAULT_DEBATE_MODEL,
) -> ReActAgent:
    """Get a solver agent.

    The default model is ``deepseek-v4-flash`` but callers can override this via
    ``model_name`` to support different debate models.
    """
    return ReActAgent(
        name=name,
        sys_prompt=f"You're a debater named {name}. Hello and welcome to the "
        "debate competition. It's not necessary to fully agree "
        "with each other's perspectives, as our objective is to "
        "find the correct answer. The debate topic is stated as "
        f"follows: {topic}. Use Chinese to answer the question",
        model=_create_chat_model(model_name),
        formatter=DashScopeChatFormatter(),
    )


def build_debater_prompt(
    role: str,
    round_id: int,
    topic: str,
) -> str:
    """Build a round-specific prompt for a debater.

    The debate follows a simple three-round structure inspired by
    British parliamentary style, with only two sides:
    - Affirmative (Proposition)
    - Opposition
    """
    assert round_id in ROUND_LABELS

    role_lower = role.lower()
    assert role_lower in ["affirmative", "opposition"]

    role_full = (
        "Affirmative (Proposition)"
        if role_lower == "affirmative"
        else "Opposition"
    )

    header = (
        f"Round {round_id} – {ROUND_LABELS[round_id]}\n"
        f"Role: {role_full} debater in a 3-round, two-side debate.\n"
        f"Motion: \"{topic}\".\n"
        "Follow a British parliamentary style adapted to two sides.\n"
    )

    if round_id == 1:
        if role_lower == "affirmative":
            body = (
                "Your task in this opening round is to:\n"
                "1. Define the issue / motion clearly.\n"
                "2. Clarify key terms if they are ambiguous.\n"
                "3. Set a clear standard / burden for winning the debate.\n"
                "4. Lay out your main positive case for the motion as numbered "
                "arguments or labeled contentions.\n"
                "5. Avoid heavy rebuttal: the Opposition has not yet given a "
                "full case. You may briefly pre-empt obvious objections, but "
                "the focus should be on building your own case.\n\n"
                "Structure your response with clear sections, for example:\n"
                "- Introduction and Motion\n"
                "- Definitions\n"
                "- Standard / Burden\n"
                "- Main Arguments (numbered)\n\n"
                "Answer in Chinese."
            )
        else:
            body = (
                "Your task in this opening round is to:\n"
                "1. Briefly respond to the Affirmative's setup (definitions "
                "and standards). You may challenge definitions if they are "
                "unreasonable or biased, and you may challenge the proposed "
                "standard / burden if it is unfair or incomplete.\n"
                "2. Keep these initial rebuttals concise and upfront; they "
                "should not dominate the speech.\n"
                "3. Present your main Opposition case against the motion.\n"
                "4. If useful, propose an alternative framework or standard "
                "for judging the debate.\n"
                "5. Focus on constructing your own positive case, not only "
                "attacking the Affirmative.\n\n"
                "Structure your response with clear sections, for example:\n"
                "- Brief Responses to Affirmative Setup\n"
                "- Opposition Framework / Standard (if any)\n"
                "- Main Opposition Arguments (numbered)\n\n"
                "Answer in Chinese."
            )
    elif round_id == 2:
        opponent = "Opposition" if role_lower == "affirmative" else "Affirmative"
        our_side = "Affirmative" if role_lower == "affirmative" else "Opposition"
        body = (
            "In this substantive second round you should:\n"
            "1. Provide direct, structured rebuttals to specific arguments the "
            f"{opponent} gave in Round 1.\n"
            "2. Introduce additional arguments or supporting material that are "
            f"consistent with the {our_side}'s position.\n"
            "3. Clarify or strengthen your earlier points when helpful.\n"
            "4. Avoid drastic redefinitions of the motion that make Round 1 "
            "irrelevant; if you challenge definitions, treat it as rebuttal.\n\n"
            "Clearly separate rebuttals from your own new material. Use "
            "headings such as:\n"
            f"- Rebuttals to {opponent}\n"
            f"- Additional Arguments for the {our_side}\n\n"
            "Answer in Chinese."
        )
    else:
        assert round_id == 3
        body = (
            "This is the final evaluation round. No new arguments are allowed.\n\n"
            "You must NOT introduce any new independent lines of reasoning, "
            "new major claims, or new major impacts. You may only:\n"
            "1. Weigh and compare the existing arguments from Rounds 1 and 2.\n"
            "2. Explain why your side's arguments are more important, more "
            "probable, or better supported than the opponent's.\n"
            "3. Highlight weaknesses or concessions in the other side, based "
            "only on points already raised.\n"
            "4. Summarize the key issues in the debate and how they resolved, "
            "then explain why your side should win under the discussed "
            "standards / burdens.\n\n"
            "If you find yourself starting a completely new argument, stop and "
            "instead frame it as weighing or explaining an argument that was "
            "already introduced earlier in the debate.\n\n"
            "Structure your response with clear sections, for example:\n"
            "- Comparative Weighing of Arguments\n"
            "- Key Issues and How They Resolved\n"
            f"- Why the {role_full} Wins\n\n"
            "Answer in Chinese."
        )

    return header + "\n" + body


# A structured output model for the moderator
class JudgeModel(BaseModel):
    """The structured output model for the moderator."""

    finished: bool = Field(
        description="Whether the debate is finished.",
    )
    evaluation: str | None = Field(
        description=(
            "A concise evaluation that compares both sides and highlights the "
            "key weighing points from the debate so far."
        ),
        default=None,
    )
    correct_answer: str | None = Field(
        description="The correct answer to the debate topic, only if the "
        "debate is finished. Otherwise, leave it as None.",
        default=None,
    )


def create_moderator_agent(
    topic: str,
    model_name: str = DEFAULT_DEBATE_MODEL,
) -> ReActAgent:
    """Get a moderator agent for the given topic."""
    return ReActAgent(
        name="Aggregator",
        sys_prompt=(
            "You're a moderator / judge for a structured three-round debate "
            "between two debaters: an Affirmative (Proposition) and an "
            "Opposition. They present and discuss their answers and "
            "perspectives on the topic:\n"
            "```\n"
            f"{topic}\n"
            "```\n"
            "The debate has at most three rounds:\n"
            "1) Opening / Main Case\n"
            "2) Rebuttals & Additional Arguments\n"
            "3) Evaluation / Weighing (no new arguments allowed)\n\n"
            "At the end of each round, you will evaluate both sides' answers "
            "and decide whether the debate is finished and you can give the "
            "correct answer. Always provide a concise evaluation in the "
            "`evaluation` field that compares both sides and highlights the "
            "key weighing points. If you decide it is finished, set "
            "`finished = true` and provide the `correct_answer`. If it is not "
            "finished yet, set `finished = false` and leave "
            "`correct_answer = null`."
        ),
        model=_create_chat_model(model_name),
        formatter=DashScopeMultiAgentFormatter(),
    )


async def run_multiagent_debate(
    topic: str,
    max_rounds: int = TOTAL_ROUNDS,
    affirmative_agent: ReActAgent | None = None,
    opposition_agent: ReActAgent | None = None,
    moderator_agent: ReActAgent | None = None,
) -> None:
    """Run the multi-agent debate workflow.

    Example:
        Topic: Whether AI will benefit humanity in the long run.
        Round 1 – Opening / Main Case:
            - Affirmative speaks first.
            - Opposition speaks second.
        Round 2 – Rebuttals & Additional Arguments:
            - Affirmative rebuttals and extensions.
            - Opposition rebuttals and extensions.
        Round 3 – Evaluation / Weighing (No New Arguments):
            - Affirmative weighs and compares existing arguments only.
            - Opposition weighs and compares existing arguments only.
    """
    # Create debate agents for this topic if they are not provided
    affirmative = affirmative_agent or create_solver_agent(
        "Affirmative",
        topic,
    )
    opposition = opposition_agent or create_solver_agent(
        "Opposition",
        topic,
    )
    moderator = moderator_agent or create_moderator_agent(topic)

    current_round = 1
    final_answer: str | None = None

    while current_round <= max_rounds:
        print(
            f"\n=== Round {current_round} – {ROUND_LABELS[current_round]} ===",
        )

        # The reply messages in MsgHub from the participants will be
        # broadcasted to all participants.
        async with MsgHub(participants=[affirmative, opposition, moderator]):
            # Affirmative always speaks first
            await affirmative(
                Msg(
                    "user",
                    build_debater_prompt(
                        "affirmative",
                        current_round,
                        topic,
                    ),
                    "user",
                ),
            )

            # Opposition responds second
            await opposition(
                Msg(
                    "user",
                    build_debater_prompt(
                        "opposition",
                        current_round,
                        topic,
                    ),
                    "user",
                ),
            )

        # Debaters do not need to know the moderator's message,
        # so moderator is called outside the MsgHub.
        msg_judge = await moderator(
            Msg(
                "user",
                (
                    "You have just observed Round "
                    f"{current_round} of a structured three-round debate. "
                    "Based on the full debate so far, provide an evaluation "
                    "and decide whether the debate is finished. "
                    "If this is not Round 3, set `finished = false` and "
                    "`correct_answer = null`. "
                    "Only in Round 3 should you set `finished = true` and "
                    "provide the final `correct_answer`."
                ),
                "user",
            ),
            structured_model=JudgeModel,
        )

        print("【JUDGE_STRUCTURED_OUTPUT】: ", msg_judge.metadata)

        if msg_judge.metadata and msg_judge.metadata.get("finished"):
            final_answer = msg_judge.metadata.get("correct_answer")
            break

        current_round += 1

    if final_answer is not None:
        print(
            "The debate is finished, and the correct answer is: ",
            final_answer,
        )
    else:
        print(
            "The debate reached the maximum number of rounds without a final "
            "answer from the moderator.",
        )


def main() -> None:
    """Entry point for running the multi-agent debate example."""
    user_topic = input(
        "Please enter the debate topic (press Enter to use the default topic): "
    ).strip()
    topic = user_topic or DEFAULT_TOPIC

    asyncio.run(run_multiagent_debate(topic))


if __name__ == "__main__":
    main()
