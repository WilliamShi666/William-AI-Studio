"""Production main-agent planning turn for Shadow Clone V2."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from typing import Any, Callable, Protocol

from . import task_pool, team_store
from .agent_tools import (
    ShadowCloneV2AgentTools,
    ShadowCloneV2ToolContext,
    register_agent_tools,
)
from .models import Task, TeamConfig


@dataclass(frozen=True)
class MainAgentToolPlanResult:
    """Result of a main-agent planning turn that used V2 tools."""

    team: TeamConfig
    tasks: list[Task]
    next_sequence: int
    execute_dynamic_plan: bool = True
    output: str = ""


@dataclass(frozen=True)
class MainAgentSynthesisResult:
    """Final user-facing answer synthesized by the main/team-lead agent."""

    output: str
    next_sequence: int | None = None


class ShadowCloneV2MainAgentPlanner(Protocol):
    """Protocol for a main-agent planning turn driven by V2 tools."""

    async def plan(
        self,
        **kwargs: Any,
    ) -> MainAgentToolPlanResult:
        """Use supplied tools to create/update team and tasks."""


class ShadowCloneV2MainAgentSynthesizer(Protocol):
    """Protocol for the final main-agent synthesis turn."""

    async def synthesize(
        self,
        **kwargs: Any,
    ) -> MainAgentSynthesisResult:
        """Use the main agent to synthesize the final user-facing response."""


MainAgentFactory = Callable[..., Any]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def build_main_agent_planning_prompt(
    *,
    user_message: str,
    user_media_refs: list[dict[str, Any]],
    thread_run_id: str,
    dynamic_team_hint: str,
) -> str:
    """Build the production main-agent planning prompt."""
    media_summary = (
        f"{len(user_media_refs)} media reference(s) attached."
        if user_media_refs
        else "No media references attached."
    )
    return "\n".join(
        [
            "## Shadow Clone V2 Main-Agent Planning Turn",
            "",
            "You are the main/team-lead agent. Build the initial Shadow Clone V2 "
            "team and living task plan by calling tools; do not invent team or "
            "task state in plain text.",
            "",
            "Required tool flow:",
            "1. Call TeamCreate with the teammates needed for this request.",
            "2. Call TaskCreate once per initial living task.",
            "3. Put the assigned teammate name in each task metadata.agent_name.",
            "4. metadata.agent_name must exactly match one TeamCreate member; "
            "do not use main, facilitator, team-lead, or any uncreated name.",
            "5. Do not create TaskCreate records for the main agent or "
            "facilitator. Main-agent coordination stays in your own planning "
            "turn and final synthesis, not in the living task pool.",
            "6. Use TaskList if you need to inspect the durable task state.",
            "7. Use SendMessage for explicit peer-to-peer coordination.",
            (
                "8. For a broadcast to all teammates, call SendMessage with "
                "recipient=\"*\" and message_type=\"broadcast\". Broadcast fan-out "
                "from the facilitator reaches every teammate; broadcast fan-out "
                "from a teammate reaches every other teammate plus the facilitator. "
                "Use broadcast only when the user or coordination state requires "
                "every applicable participant to receive the same instruction."
            ),
            (
            "9. TaskCreate descriptions for follow-up, wake, or idle-reuse "
            "tasks must reference prior work by artifact path or short "
            "summary only; do not copy prior delivery marker strings or prior "
            "output text into the new task description. If the current user "
            "request defines new required output markers, include only those "
            "new required output markers in the new task instructions."
            ),
            (
                "10. Return this only after the durable TeamCreate, TaskCreate, and SendMessage tool calls: "
                "return a concise user-visible natural-language "
                "planning update for the chat panel. This text is for the user "
                "experience only: durable state must come only from tools. It "
                "must read like assistant planning/orchestration prose, not "
                "system progress or an event log."
            ),
            "",
            "Policy:",
            dynamic_team_hint,
            "",
            "Run context:",
            f"- thread_run_id: {thread_run_id}",
            f"- media: {media_summary}",
            "",
            "User request:",
            user_message,
        ]
    )


def build_main_agent_synthesis_prompt(
    *,
    user_message: str,
    tasks: list[dict[str, Any]],
    team: dict[str, Any],
    messages: list[dict[str, Any]],
    worker_outputs: list[dict[str, Any]],
    thread_run_id: str,
) -> str:
    """Build the production main-agent final synthesis prompt."""
    return "\n".join(
        [
            "## Shadow Clone V2 Main-Agent Final Synthesis Turn",
            "",
            "You are the main/team-lead agent. Synthesize a final user-facing "
            "answer from the living task outcomes below. Do not merely concatenate "
            "worker outputs; resolve conflicts, remove duplication, and provide a "
            "coherent response to the original user request.",
            "",
            f"thread_run_id: {thread_run_id}",
            "",
            "Original user request:",
            user_message,
            "",
            "Current task state:",
            json_dumps(tasks),
            "",
            "Current team state:",
            json_dumps(team),
            "",
            "Relevant peer messages:",
            json_dumps(messages),
            "",
            "Worker task outcomes:",
            json_dumps(worker_outputs),
        ]
    )


def json_dumps(value: Any) -> str:
    """Serialize prompt context deterministically."""
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


async def _default_agentscope_main_agent_factory(
    *,
    model_key: str,
    tools: ShadowCloneV2AgentTools,
    tool_context: ShadowCloneV2ToolContext,
    db_client: object | None = None,
    trace: object | None = None,
    toolkit_factory: Callable[[], Any] | None = None,
    model_factory: Any | None = None,
    memory_factory: Callable[[], Any] | None = None,
    agent_class: Callable[..., Any] | None = None,
    **_: Any,
) -> Any:
    """Build a production AgentScope-compatible main agent for planning."""
    if memory_factory is None:
        from agentscope.memory import InMemoryMemory

        memory_factory = InMemoryMemory
    if toolkit_factory is None:
        from agentscope.tool import Toolkit

        toolkit_factory = Toolkit
    if agent_class is None:
        from agentscope_integration.agents.metadata_aware_react_agent import (
            MetadataAwareReActAgent as ReActAgent,
        )

        agent_class = ReActAgent
    if model_factory is None:
        from agentscope_integration.models import ModelFactory

        model_factory = ModelFactory

    if "/" in str(model_key or ""):
        model, formatter = model_factory.create_from_full_name(
            model_name=model_key,
            trace=trace,
        )
    else:
        model, formatter = model_factory.create(
            model_key=model_key,
            trace=trace,
        )
    toolkit = toolkit_factory()
    register_agent_tools(toolkit, context=tool_context, tools=tools)
    agent = agent_class(
        name="ShadowCloneV2MainAgent",
        sys_prompt=(
            "You are the Shadow Clone V2 main/team-lead planning agent. "
            "You must use the registered Team/Task/Message tools to create "
            "durable state. Do not answer the user's substantive request "
            "directly during planning, but after durable tool calls you should "
            "return a concise user-visible natural-language planning update. "
            "If the request lacks source material, still create an intake or "
            "research task instead of asking a plain-text follow-up. For "
            "follow-up, wake, or idle-reuse tasks, do not copy prior delivery "
            "marker strings or prior output text into TaskCreate descriptions; "
            "reference prior work by artifact path or short summary and include "
            "only the current request's new required output markers."
        ),
        model=model,
        formatter=formatter,
        toolkit=toolkit,
        memory=memory_factory(),
        long_term_memory=None,
        long_term_memory_mode="static_control",
        max_iters=20,
        parallel_tool_calls=False,
    )
    setattr(agent, "_shadow_clone_v2_agent_tools", tools)
    return agent


async def _default_agentscope_main_synthesis_agent_factory(
    *,
    model_key: str,
    db_client: object | None = None,
    trace: object | None = None,
    model_factory: Any | None = None,
    memory_factory: Callable[[], Any] | None = None,
    agent_class: Callable[..., Any] | None = None,
    **_: Any,
) -> Any:
    """Build a production AgentScope-compatible main agent for final synthesis."""
    if memory_factory is None:
        from agentscope.memory import InMemoryMemory

        memory_factory = InMemoryMemory
    if agent_class is None:
        from agentscope_integration.agents.metadata_aware_react_agent import (
            MetadataAwareReActAgent as ReActAgent,
        )

        agent_class = ReActAgent
    if model_factory is None:
        from agentscope_integration.models import ModelFactory

        model_factory = ModelFactory

    if "/" in str(model_key or ""):
        model, formatter = model_factory.create_from_full_name(
            model_name=model_key,
            trace=trace,
        )
    else:
        model, formatter = model_factory.create(
            model_key=model_key,
            trace=trace,
        )
    return agent_class(
        name="ShadowCloneV2MainAgent",
        sys_prompt=(
            "You are the Shadow Clone V2 main/team-lead agent. "
            "Write the final answer by synthesizing task outcomes. "
            "Do not expose internal coordination details unless they are useful "
            "to answer the user."
        ),
        model=model,
        formatter=formatter,
        toolkit=None,
        memory=memory_factory(),
        long_term_memory=None,
        long_term_memory_mode="static_control",
        max_iters=8,
        parallel_tool_calls=False,
    )


class AgentScopeShadowCloneV2MainAgentPlanner:
    """Run default Shadow Clone V2 planning through an AgentScope main turn."""

    def __init__(
        self,
        *,
        agent_factory: MainAgentFactory | None = None,
    ) -> None:
        self.agent_factory = agent_factory or _default_agentscope_main_agent_factory

    async def plan(
        self,
        **kwargs: Any,
    ) -> MainAgentToolPlanResult:
        tools = kwargs["tools"]
        if not isinstance(tools, ShadowCloneV2AgentTools):
            raise TypeError("tools must be a ShadowCloneV2AgentTools instance")
        tool_context = tools.context
        user_media_refs = list(kwargs.get("user_media_refs") or [])
        prompt = build_main_agent_planning_prompt(
            user_message=str(kwargs.get("user_message") or ""),
            user_media_refs=user_media_refs,
            thread_run_id=str(kwargs.get("thread_run_id") or ""),
            dynamic_team_hint=str(kwargs.get("dynamic_team_hint") or ""),
        )
        agent = await _maybe_await(
            self.agent_factory(
                model_key=str(kwargs.get("model_key") or ""),
                prompt=prompt,
                tools=tools,
                tool_context=tool_context,
                db_client=kwargs.get("db_client"),
                trace=kwargs.get("trace"),
                user_message=kwargs.get("user_message"),
                user_media_refs=user_media_refs,
                thread_run_id=kwargs.get("thread_run_id"),
                created_at=kwargs.get("created_at"),
                dynamic_team_hint=kwargs.get("dynamic_team_hint"),
            )
        )

        from agentscope.message import Msg

        response = await _maybe_await(
            agent(
                Msg(
                    name="ShadowCloneV2MainAgent",
                    content=prompt,
                    role="user",
                )
            )
        )
        team = await team_store.read_team(
            project_id=tool_context.project_id,
            thread_id=tool_context.thread_id,
            account_id=tool_context.account_id,
        )
        if team is None:
            raise RuntimeError("main-agent planning did not create a team")
        tasks = await task_pool.list_tasks(run_id=tool_context.run_id)
        if not tasks:
            raise RuntimeError("main-agent planning did not create any tasks")
        _validate_tool_created_plan(team=team, tasks=list(tasks))
        return MainAgentToolPlanResult(
            team=team,
            tasks=list(tasks),
            next_sequence=tools.next_sequence,
            execute_dynamic_plan=True,
            output=_response_to_text(response),
        )


class AgentScopeShadowCloneV2MainAgentSynthesizer:
    """Run final answer synthesis through an AgentScope main-agent turn."""

    def __init__(
        self,
        *,
        agent_factory: MainAgentFactory | None = None,
    ) -> None:
        self.agent_factory = (
            agent_factory or _default_agentscope_main_synthesis_agent_factory
        )

    async def synthesize(
        self,
        **kwargs: Any,
    ) -> MainAgentSynthesisResult:
        worker_outputs = list(kwargs.get("worker_outputs") or [])
        tasks = _json_ready_list(kwargs.get("tasks"))
        messages = _json_ready_list(kwargs.get("messages"))
        if not worker_outputs and not tasks and not messages:
            return MainAgentSynthesisResult(output="", next_sequence=None)
        prompt = build_main_agent_synthesis_prompt(
            user_message=str(kwargs.get("user_message") or ""),
            tasks=tasks,
            team=_json_ready_object(kwargs.get("team")),
            messages=messages,
            worker_outputs=worker_outputs,
            thread_run_id=str(kwargs.get("thread_run_id") or ""),
        )
        agent = await _maybe_await(
            self.agent_factory(
                model_key=str(kwargs.get("model_key") or ""),
                prompt=prompt,
                worker_outputs=worker_outputs,
                db_client=kwargs.get("db_client"),
                trace=kwargs.get("trace"),
                user_message=kwargs.get("user_message"),
                thread_run_id=kwargs.get("thread_run_id"),
                created_at=kwargs.get("created_at"),
            )
        )

        from agentscope.message import Msg

        response = await _maybe_await(
            agent(
                Msg(
                    name="ShadowCloneV2MainAgent",
                    content=prompt,
                    role="user",
                )
            )
        )
        return MainAgentSynthesisResult(
            output=_response_to_text(response),
            next_sequence=kwargs.get("sequence_start"),
        )


def _response_to_text(response: Any) -> str:
    """Extract text from an AgentScope response-like object."""
    if response is None:
        return ""
    get_text_content = getattr(response, "get_text_content", None)
    if callable(get_text_content):
        try:
            text = get_text_content()
            if isinstance(text, str):
                return text
        except Exception:
            pass
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                chunks.append(str(getattr(block, "text", "")))
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content or response)


def _json_ready_object(value: Any) -> dict[str, Any]:
    """Convert pydantic/dataclass-like objects to JSON-ready dicts."""
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {"value": str(value)}


def _json_ready_list(value: Any) -> list[dict[str, Any]]:
    """Convert a sequence of objects to JSON-ready dictionaries."""
    items = list(value or [])
    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(item)
            continue
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            normalized.append(model_dump(mode="json"))
            continue
        normalized.append({"value": str(item)})
    return normalized


def _validate_tool_created_plan(*, team: TeamConfig, tasks: list[Task]) -> None:
    member_names = {member.agent_name for member in team.members}
    for task in tasks:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        agent_name = str(metadata.get("agent_name") or "").strip()
        if not agent_name:
            raise RuntimeError(
                f"main-agent planning created task {task.id} without "
                "metadata.agent_name"
            )
        if agent_name not in member_names:
            raise RuntimeError(
                f"main-agent planning created task {task.id} for unknown agent "
                f"{agent_name}"
            )
