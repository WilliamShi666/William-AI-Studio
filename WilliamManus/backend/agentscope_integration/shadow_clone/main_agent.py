"""Main Agent wrapper for Shadow Clone execution, optional decomposition, and synthesis."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from agentscope.message import Msg, TextBlock
from agentscope.model import ChatResponse
from agentscope.tool import ToolResponse


from agentscope_integration.agents.metadata_aware_react_agent import (
    MetadataAwareReActAgent as ReActAgent,
)
from agentscope_integration.adapters.thread_manager_adapter import ThreadManagerAdapter
from agentscope_integration.context_builder import ContextBuilder
from agentscope_integration.memory import (
    MessagesTableMemory,
    create_long_term_memory,
    load_ltm_settings_from_env,
)
from agentscope_integration.models import ModelFactory
from agentscope_integration.prompts.orchestrator_prompt import get_orchestrator_prompt
from agentscope_integration.tools import ToolkitAdapter
from agentscope_integration.utils.workspace_guard import relocate_external_paths
from utils.logger import logger

from .constants import (
    SHADOW_CLONE_MAIN_MODEL,
    SHADOW_CLONE_MAX_CONCURRENT,
    SHADOW_CLONE_MAX_SUBAGENTS,
    SHADOW_CLONE_PLAN_BATCH_TARGET,
    ShadowCloneMode,
)
from .dag_executor import (
    topological_sort_layers,
    validate_dependencies,
    validate_layer_widths,
    validate_subtasks,
)
from .result_store import read_full_result as read_full_result_entry, read_summaries


_TURN_STREAM_END = object()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _msg_to_text(message: Any) -> str:
    if message is None:
        return ""
    try:
        get_text_content = getattr(message, "get_text_content", None)
    except Exception:
        get_text_content = None
    if callable(get_text_content):
        try:
            text = get_text_content()
            if isinstance(text, str):
                return text
        except Exception:
            pass
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        fragments: List[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                fragments.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                fragments.append(str(getattr(block, "text", "")))
        return "\n".join([item for item in fragments if item])
    return str(content or "")


def _shorten_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."



def _normalize_dependencies(raw_dependencies: Any) -> List[Dict[str, str]]:
    """
    Normalize dependency payloads produced by LLM tool calls.

    Accepted formats:
    1) [{"from_id": "task_a", "to_id": "task_b"}]
    2) [{"subtask_id": "task_b", "depends_on": ["task_a", "task_c"]}]
    """
    if raw_dependencies is None:
        return []
    if not isinstance(raw_dependencies, list):
        raise ValueError("dependencies must be a list")

    normalized: List[Dict[str, str]] = []
    seen_edges: set[tuple[str, str]] = set()

    for index, dep in enumerate(raw_dependencies):
        if not isinstance(dep, dict):
            raise ValueError(f"dependency[{index}] must be a dict")

        from_id = str(dep.get("from_id", "")).strip()
        to_id = str(dep.get("to_id", "")).strip()
        if from_id and to_id:
            edge = (from_id, to_id)
            if edge not in seen_edges:
                seen_edges.add(edge)
                normalized.append({"from_id": from_id, "to_id": to_id})
            continue

        target_id = str(dep.get("subtask_id", "") or dep.get("to_id", "")).strip()
        depends_on = dep.get("depends_on")
        if target_id and depends_on is not None:
            if isinstance(depends_on, str):
                source_ids = [depends_on]
            elif isinstance(depends_on, list):
                source_ids = [str(item).strip() for item in depends_on if str(item).strip()]
            else:
                raise ValueError(
                    f"dependency[{index}] depends_on must be a string or list",
                )

            if not source_ids:
                raise ValueError(
                    f"dependency[{index}] depends_on must include at least one source",
                )

            for source_id in source_ids:
                edge = (source_id, target_id)
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                normalized.append({"from_id": source_id, "to_id": target_id})
            continue

        raise ValueError(
            f"dependency[{index}] must use either from_id/to_id or subtask_id/depends_on",
        )

    return normalized


def _spawn_subagents_example_payload() -> Dict[str, Any]:
    return {
        "subtasks": [
            {
                "id": "task_a",
                "role": "Researcher",
                "task_description": "Research the first independent slice of the task.",
            },
            {
                "id": "task_b",
                "role": "Analyst",
                "task_description": "Analyze the second independent slice of the task.",
            },
        ],
        "dependencies": [],
    }


def _spawn_subagents_json_schema() -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": "spawn_subagents",
            "description": (
                "Create the Shadow Clone execution proposal. Call this exactly once "
                "with a non-empty subtasks array. Never call it with empty input."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "subtasks": {
                        "type": "array",
                        "description": (
                            "Required. Non-empty list of subtasks to run in Shadow Clone."
                        ),
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "id": {
                                    "type": "string",
                                    "description": "Stable subtask id such as task_a or company_openai.",
                                },
                                "role": {
                                    "type": "string",
                                    "description": "Short role/name for the subagent.",
                                },
                                "task_description": {
                                    "type": "string",
                                    "description": "Detailed instructions for the subagent to execute.",
                                },
                            },
                            "required": ["id", "role", "task_description"],
                        },
                        "examples": [_spawn_subagents_example_payload()["subtasks"]],
                    },
                    "dependencies": {
                        "type": "array",
                        "description": (
                            "Optional dependency edges. Use [] when subtasks are independent. "
                            "Each edge uses from_id -> to_id."
                        ),
                        "default": [],
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "from_id": {
                                    "type": "string",
                                    "description": "Completed predecessor subtask id.",
                                },
                                "to_id": {
                                    "type": "string",
                                    "description": "Dependent subtask id that waits for from_id.",
                                },
                            },
                            "required": ["from_id", "to_id"],
                        },
                        "examples": [[]],
                    },
                },
                "required": ["subtasks"],
            },
        },
    }


class MainAgent:
    """Main Shadow Clone thread with full direct execution plus optional subagent spawning."""

    def __init__(
        self,
        *,
        thread_id: str,
        project_id: str,
        agent_run_id: str,
        model_key: Optional[str],
        db_client,
        mode: ShadowCloneMode,
        on_proposal_captured: Optional[Callable[[Dict[str, Any]], None]] = None,
        trace=None,
    ) -> None:
        self.thread_id = thread_id
        self.project_id = project_id
        self.agent_run_id = agent_run_id
        self.model_key = model_key or SHADOW_CLONE_MAIN_MODEL
        self.db_client = db_client
        self.mode = mode
        self._lf_trace = trace

        self._initialized = False
        self._main_agent: Optional[ReActAgent] = None
        self._decompose_agent: Optional[ReActAgent] = None
        self._direct_agent: Optional[ReActAgent] = None
        self._aggregate_agent: Optional[ReActAgent] = None
        self._recovery_agent: Optional[ReActAgent] = None
        self._failure_review_agent: Optional[ReActAgent] = None
        self._memory: Optional[MessagesTableMemory] = None
        self._proposal: Optional[Dict[str, Any]] = None
        self._layer_recovery_decision: Optional[Dict[str, Any]] = None
        self._last_response_text: str = ""
        self._last_aggregate_text: str = ""
        self._long_term_memory = None
        self._ltm_entered = False
        self._ltm_settings = load_ltm_settings_from_env()
        self._spawn_validation_failures: int = 0
        self._spawn_allowed: bool = True
        self._on_proposal_captured = on_proposal_captured
        self._shared_toolkit_adapter: Optional[ToolkitAdapter] = None

    def _ltm_backend_hint(self) -> str:
        config_path = str(self._ltm_settings.reme_config_path or "").lower()
        if not config_path:
            return "default_reme_config(memory-likely)"
        if "qdrant" in config_path:
            return "qdrant_configured"
        return "custom_configured"

    async def _close_long_term_memory(self) -> None:
        if not self._long_term_memory or not self._ltm_entered:
            return
        try:
            await self._long_term_memory.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning(
                "[ShadowClone] Failed to close main-agent long-term memory run_id=%s: %s",
                self.agent_run_id,
                exc,
            )
        finally:
            self._ltm_entered = False
            self._long_term_memory = None

    def _build_main_prompt(self) -> str:
        if self.mode == ShadowCloneMode.ON:
            mode_policy = (
                "Shadow Clone mode is ON. Prefer `spawn_subagents` when layered or parallel "
                "execution has clear value, but keep direct execution for small follow-up work."
            )
        else:
            mode_policy = (
                "Decide whether to execute directly or use `spawn_subagents`. "
                "Use direct execution for small or tightly scoped work; use subagents only "
                "when the task clearly benefits from decomposition."
            )

        shadow_clone_overlay = (
            "## Shadow Clone Main Thread Overlay\n"
            "You are operating as the Shadow Clone main thread.\n"
            "You retain FULL direct execution ability with the normal toolchain. "
            "You are not a planner-only agent.\n"
            "`delegate_to_worker` is NOT available in this mode even if mentioned elsewhere "
            "in the shared prompt. Ignore any instruction that tells you to call it.\n"
            f"{mode_policy}\n\n"
            "### Execute Directly For Small Follow-Up Work\n"
            "- If the user only wants a small file edit, a light rewrite, or a limited change to one or a few files, execute directly.\n"
            "- If the task can be completed with a short direct tool sequence, execute directly.\n"
            "- If prior Shadow Clone work already produced files in `/workspace`, prefer editing those files directly instead of spawning a new Shadow Clone round.\n"
            "- If the user explicitly says not to use Shadow Clone when you can handle it yourself, prefer direct execution.\n\n"
            "### When To Use `spawn_subagents`\n"
            "- Use `spawn_subagents` only when the work has clear parallel or layered benefit.\n"
            "- Good candidates: multiple independent research slices, multi-layer DAGs, or distinct specialist roles whose outputs must later be synthesized.\n"
            "- Do NOT use `spawn_subagents` for trivial follow-up edits or small direct tasks.\n"
            f"- For bulk workloads, keep naturally independent work in the same layer when the fanout is at most {SHADOW_CLONE_MAX_CONCURRENT}.\n"
            f"- Only split work into additional layers or waves when there are real dependencies or the natural fanout exceeds {SHADOW_CLONE_MAX_CONCURRENT}.\n"
            f"- Never put more than {SHADOW_CLONE_MAX_CONCURRENT} subtasks in the same layer. If the workload is larger, add more layers and explicit dependencies.\n"
            "- You may call `spawn_subagents` at most ONCE for a given user turn.\n\n"
            "### Exact Tool Call Shape (MANDATORY)\n"
            "When you call `spawn_subagents`, you MUST provide a non-empty `subtasks` array. "
            "Never call the tool with `{}` or empty input. Think first, then call the tool once with JSON like:\n"
            f"{json.dumps(_spawn_subagents_example_payload(), ensure_ascii=False)}\n\n"
            "### Fine-Grained DAG Design (follow natural dependencies)\n"
            f"If a set of subtasks is naturally independent and the group is no wider than {SHADOW_CLONE_MAX_CONCURRENT}, keep them in the same layer.\n"
            "Only split work into additional layers or waves when dependencies require it or the natural fanout is wider than the concurrency cap.\n"
            "When layers are needed, prefer dependency-shaped DAGs over arbitrary batching:\n"
            "  Layer 0: Data collection / independent research tasks\n"
            "  Layer 1: Preliminary analysis / cross-validation of Layer 0 results\n"
            "  Layer 2: Synthesis / comparative analysis\n"
            "  Layer 3 (optional): Final report generation\n\n"
            "### Cross-Reference in Task Descriptions\n"
            "Each subtask's task_description should reference related subtasks by id "
            "so the agent understands the broader context.\n\n"
            "### Dependency-Driven Data Flow\n"
            "Later-layer subtasks automatically receive access to earlier-layer results "
            "via the `read_full_result` tool. Design your layers to exploit this:\n"
            "- If two subtasks need each other's output, place them in different layers\n"
            "- Avoid information dependencies between subtasks in the same layer\n"
            "- Later-layer subtasks can read any completed earlier-layer result\n\n"
            "### Sandbox Coordination\n"
            "Shadow Clone subagents for the same run/project reuse the same underlying sandbox when one already exists. "
            "Files written in /workspace can therefore persist across phases and may be visible to other subagents as incidental context. "
            "However, same-layer subtasks can run concurrently, so shared /workspace state is non-authoritative and must NOT be used as an implicit synchronization mechanism. "
            "If one subtask needs deterministic artifacts from another, place them in different layers, make the dependency explicit, use `read_full_result(subtask_id)`, and recreate or verify any required files before using them.\n\n"
            "### Example: Research 3 Companies\n"
            "  L0: task_a (research Co.A), task_b (research Co.B), task_c (research Co.C)\n"
            "  L1: task_d (cross-compare A vs B vs C, depends on a,b,c)\n"
            "  L2: task_e (synthesize final report, depends on d)\n\n"
            "### Example: Generate 50 Posters\n"
            f"  Use 5 waves x up to {SHADOW_CLONE_MAX_CONCURRENT} poster subtasks per layer,\n"
            "  rather than 50 same-layer subtasks. Only add review or consistency subtasks\n"
            "  between waves when earlier results should influence later prompts.\n"
            "\n### Workspace File Collection (applies to subagent task descriptions)\n"
            "When writing `task_description` for each subtask, include this instruction:\n"
            "\"Before finishing, verify all produced files are under /workspace. "
            "If any output was created outside /workspace (e.g. /tmp, /home, /root), "
            "copy it into /workspace. Report only /workspace paths. If the deliverable is "
            "a nested folder or directory tree, report the root folder path. For multi-file "
            "deliverables, keep the directory structure intact under /workspace.\"\n"
            "This ensures subagents proactively collect their output into the user-accessible location.\n"
        )
        return get_orchestrator_prompt() + "\n\n" + shadow_clone_overlay

    def _build_decompose_prompt(self) -> str:
        """Compatibility shim for tests and prompt inspection."""
        return self._build_main_prompt()

    def _build_direct_execution_prompt(self) -> str:
        return (
            get_orchestrator_prompt()
            + "\n\n"
            + "## Shadow Clone Main Thread Continuation Overlay\n"
            "You are operating as the Shadow Clone main thread after the planning phase.\n"
            "You retain FULL direct execution ability with the normal toolchain.\n"
            "`spawn_subagents` is NOT available in this continuation turn.\n"
            "Continue solving the active user request directly with your normal tools.\n"
            "Prefer concise tool sequences for small follow-up edits, targeted file changes, and final refinements in `/workspace`.\n"
        )

    def _build_aggregate_prompt(self) -> str:
        """Compatibility shim for tests and prompt inspection."""
        return (
            self._build_direct_execution_prompt()
            + "\n\n## Post-Subtask Continuation Guidance\n"
            "All Shadow Clone subtasks have finished before this continuation begins.\n"
            "Use `read_results` for the summary table, `read_full_result(subtask_id)` for detailed outputs, "
            "and your normal tools to refine final deliverables in `/workspace`.\n"
            "Do not start a second Shadow Clone round in this continuation turn.\n"
            "When reviewing subtask summaries, identify information gaps, incomplete work, "
            "or contradictions. Resolve them directly when possible, and note suggested "
            "follow-up work when gaps remain.\n\n"
            "### Post-Subtask Workspace Verification (MANDATORY)\n"
            "After reviewing subtask results, verify that ALL deliverable files exist under `/workspace`.\n"
            "If any subtask produced files outside `/workspace` (e.g. in `/tmp`, `/home`, `/root`), "
            "copy them into `/workspace` now using `cp -R <source> /workspace/`.\n"
            "Run `find /tmp /home /root -maxdepth 3 -newer /proc/1/cmdline -type f 2>/dev/null` "
            "to discover any stray files. Copy task-relevant ones to `/workspace`.\n"
            "\n### Mandatory Delivery Fallback (MANDATORY)\n"
            "If every deliverable is directly under /workspace, direct delivery is allowed and no zip is required.\n"
            "If any deliverable is nested, is a directory tree, or you are unsure, create "
            "`/workspace/shadow_clone_deliverables.zip` that preserves the original structure.\n"
            "Also create `/workspace/shadow_clone_deliverables_manifest.txt` listing the delivered files and directories.\n"
            "Preserve originals instead of moving or deleting them after packaging.\n"
            "Prefer root-level `/workspace` download paths in the final response.\n"
            "Only reference `/workspace/...` paths in your final response to the user."
        )

    def _build_denial_prompt(self) -> str:
        return (
            self._build_direct_execution_prompt()
            + "\n\n## Denial Continuation Guidance\n"
            "The user denied the Shadow Clone proposal.\n"
            "Continue the current request yourself using only the normal toolchain.\n"
            "Do not attempt another Shadow Clone round in this continuation turn.\n"
        )

    def _build_recovery_prompt(self) -> str:
        return (
            self._build_direct_execution_prompt()
            + "\n\n## Recovery Continuation Guidance\n"
            "Shadow Clone execution encountered a recovery event after planning started.\n"
            "This may be a sandbox continuity failure or a layer that finished with failed subtasks.\n"
            "Continue the request directly using preserved completed subtask results, durable `/workspace` artifacts, "
            "and any non-sandbox tools that remain available.\n"
            "If a sandbox-backed tool fails again, stop relying on live sandbox state and switch to preserved results "
            "or durable artifacts instead of retrying the same environment-dependent action.\n"
            "Do not call `spawn_subagents` again in this continuation turn.\n"
        )

    def _build_failure_review_prompt(self) -> str:
        return (
            "## Shadow Clone Failed-Layer Review\n"
            "You are reviewing a completed Shadow Clone layer where one or more subtasks failed.\n"
            "This review happens after the current layer has reached terminal states. You are NOT doing normal tool work.\n"
            "Use `read_results` and `read_full_result(subtask_id)` if you need more detail.\n"
            "Then call `resolve_failed_subtasks` exactly once with one of these actions:\n"
            "- `retry_failed_subtasks`: use only when the failed subtasks look retryable as new attempts without changing task scope.\n"
            "- `continue_with_partial_results`: use when retries are unlikely to help, the failure is semantic/tooling/provider-related, or partial results are already sufficient.\n"
            "Be conservative. Do not request another Shadow Clone planning round.\n"
            "Do not use live `/workspace` assumptions as evidence of deterministic success.\n"
        )

    @staticmethod
    def _disable_console_output(agent: Any) -> None:
        if hasattr(agent, "set_console_output_enabled"):
            try:
                agent.set_console_output_enabled(False)
            except Exception:
                pass

    @staticmethod
    def _get_content_blocks(message: Any) -> List[Dict[str, Any]]:
        if hasattr(message, "get_content_blocks"):
            try:
                blocks = message.get_content_blocks() or []
                return [block if isinstance(block, dict) else dict(block) for block in blocks]
            except Exception:
                return []
        return []

    def _is_internal_spawn_subagents_message(self, message: Any) -> bool:
        blocks = self._get_content_blocks(message)
        if not blocks:
            return False

        tool_blocks = [
            block
            for block in blocks
            if str(block.get("type") or "").strip() in {"tool_use", "tool_result"}
        ]
        if not tool_blocks:
            return False

        tool_names = {
            str(block.get("name") or "").strip()
            for block in tool_blocks
            if str(block.get("name") or "").strip()
        }
        return bool(tool_names) and tool_names == {"spawn_subagents"}

    @staticmethod
    def _build_compact_subtask_result_rows(
        rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        compact_rows: List[Dict[str, Any]] = []
        for row in rows:
            compact: Dict[str, Any] = {}
            for field in ("subtask_id", "role", "status"):
                value = row.get(field)
                if value not in (None, ""):
                    compact[field] = str(value)

            summary_text = _shorten_text(
                row.get("summary")
                or row.get("result_summary")
                or row.get("final_answer")
                or row.get("output_summary")
                or "",
                max_chars=280,
            )
            if summary_text:
                compact["summary"] = summary_text

            error_text = _shorten_text(
                row.get("error")
                or row.get("failure_reason")
                or "",
                max_chars=180,
            )
            if error_text:
                compact["error"] = error_text

            artifact_count = row.get("artifact_count")
            if artifact_count is None:
                artifacts = row.get("artifacts")
                if isinstance(artifacts, list):
                    artifact_count = len(artifacts)
            if artifact_count not in (None, ""):
                compact["artifact_count"] = artifact_count

            compact_rows.append(compact or {"subtask_id": str(row.get("subtask_id") or "")})
        return compact_rows

    def _build_subtask_results_message(self, rows: List[Dict[str, Any]]) -> str:
        compact_rows = self._build_compact_subtask_result_rows(rows)
        status_counts: Dict[str, int] = {}
        for row in compact_rows:
            status = str(row.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        inline_json = json.dumps(compact_rows, ensure_ascii=False, default=str)
        return (
            "Shadow Clone subtask execution has finished.\n\n"
            f"Summary counts by status: {json.dumps(status_counts, ensure_ascii=False, sort_keys=True)}\n"
            "Compact subtask digest (intentionally slimmed; use tools for omitted detail):\n"
            f"```json\n{inline_json}\n```\n\n"
            "Continue the user's request directly now. "
            "Use `read_results` to inspect the summary table again, `read_full_result(subtask_id)` "
            "for deeper detail, and your normal tools to update files in `/workspace`. "
            "Do not call `spawn_subagents` again in this continuation turn.\n"
            "Fallback reminder: if nested or uncertain deliverables remain, create "
            "/workspace/shadow_clone_deliverables.zip and /workspace/shadow_clone_deliverables_manifest.txt.\n"
            "IMPORTANT: Before responding to the user, verify ALL deliverable files are under "
            "/workspace. If any are outside /workspace, copy them in now. If nested, directory, or "
            "uncertain deliverables remain, create /workspace/shadow_clone_deliverables.zip and "
            "/workspace/shadow_clone_deliverables_manifest.txt, and preserve the original files."
        )

    @staticmethod
    def _build_denial_message() -> str:
        return (
            "The user denied the Shadow Clone proposal. Continue solving the current request "
            "directly using your normal tools. Do not call `spawn_subagents` again in this "
            "continuation turn."
        )

    def _register_workspace_guard(self, agent: ReActAgent) -> None:
        async def _workspace_guard(hooked_agent, kwargs):
            msg = kwargs.get("msg")
            last = kwargs.get("last", True)
            if not last or not msg or getattr(msg, "role", None) != "assistant":
                return None
            updated_msg, replacements = await relocate_external_paths(
                msg,
                hooked_agent.toolkit,
                label="shadow-main",
            )
            if replacements:
                patched = dict(kwargs)
                patched["msg"] = updated_msg
                return patched
            return None

        agent.register_instance_hook(
            hook_type="pre_print",
            hook_name="workspace_guard",
            hook=_workspace_guard,
        )

    def _register_micro_compact(self, agent: ReActAgent, memory) -> None:
        """Register Micro Compact hook for Shadow Clone agents."""
        from ..memory.micro_compact import make_micro_compact_hook
        mc_enabled = getattr(memory, 'enable_micro_compact', True)
        agent.register_instance_hook(
            hook_type="pre_reasoning",
            hook_name="micro_compact",
            hook=make_micro_compact_hook(enabled=mc_enabled),
        )

    async def setup(self) -> None:
        if self._initialized:
            return

        context_builder = ContextBuilder.from_env()
        # Link the main shadow-clone model to orchestrator-system prompt for Metrics attribution.
        _lf_main_prompt_obj = None
        try:
            from agentscope_integration.prompts.orchestrator_prompt import get_orchestrator_prompt_object
            _lf_main_prompt_obj = get_orchestrator_prompt_object()
        except Exception:
            pass
        model, formatter = ModelFactory.create(
            model_key=self.model_key,
            trace=self._lf_trace,
            prompt=_lf_main_prompt_obj,
        )
        memory = MessagesTableMemory(
            db_client=self.db_client,
            **context_builder.build_orchestrator_memory_kwargs(
                thread_id=self.thread_id,
                project_id=self.project_id,
                model_key=self.model_key,
            ),
        )
        setattr(memory, "_save_thread_run_id", self.agent_run_id)
        aggregate_memory = MessagesTableMemory(
            db_client=self.db_client,
            **context_builder.build_shadow_clone_tail_memory_kwargs(
                thread_id=self.thread_id,
                project_id=self.project_id,
                thread_run_id=self.agent_run_id,
                model_key=self.model_key,
            ),
        )
        setattr(aggregate_memory, "_save_thread_run_id", self.agent_run_id)
        thread_manager = ThreadManagerAdapter(db_client=self.db_client)

        def spawn_subagents(
            subtasks: list[dict] | None = None,
            dependencies: list[dict] | None = None,
        ) -> ToolResponse:
            if not self._spawn_allowed:
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                "`spawn_subagents` is not available in this continuation turn. "
                                "Continue directly using your normal tools."
                            ),
                        ),
                    ],
                )

            example_payload = json.dumps(
                _spawn_subagents_example_payload(),
                ensure_ascii=False,
            )

            if not subtasks:
                self._spawn_validation_failures += 1
                message = (
                    "Error: `subtasks` is required and must be a non-empty array. "
                    "Call `spawn_subagents` exactly once with JSON like: "
                    f"{example_payload}"
                )
                if self._spawn_validation_failures >= 2:
                    message += (
                        " Repeated invalid call detected. Stop repeating the empty call, "
                        "finish planning first, then provide a complete `subtasks` array."
                    )
                return ToolResponse(
                    content=[TextBlock(type="text", text=message)],
                )

            try:
                dep_list = _normalize_dependencies(dependencies or [])
                validate_subtasks(subtasks)
                if len(subtasks) > SHADOW_CLONE_MAX_SUBAGENTS:
                    raise ValueError(
                        f"too many subtasks: {len(subtasks)} exceeds limit {SHADOW_CLONE_MAX_SUBAGENTS}",
                    )
                validate_dependencies(subtasks, dep_list)
                validate_layer_widths(
                    subtasks,
                    dep_list,
                    max_layer_width=SHADOW_CLONE_MAX_CONCURRENT,
                )
            except Exception as validation_error:
                self._spawn_validation_failures += 1
                message = (
                    "Error: invalid `spawn_subagents` input: "
                    f"{validation_error}. Each subtask must include `id`, `role`, "
                    "and `task_description`. Dependencies use edge objects like "
                    '`{"from_id":"task_a","to_id":"task_b"}`.'
                )
                validation_text = str(validation_error)
                if "same-layer fanout" in validation_text:
                    try:
                        natural_layers = topological_sort_layers(
                            subtasks,
                            dep_list,
                            max_parallelism=None,
                        )
                        widest_layer = max((len(layer) for layer in natural_layers), default=0)
                    except Exception:
                        widest_layer = None
                    message += (
                        " Replan the workload into multiple layers or waves only where needed."
                        f" Keep naturally independent work together up to {SHADOW_CLONE_MAX_CONCURRENT}"
                        " subtasks per layer, and split only when dependencies or fanout require it."
                    )
                    if widest_layer is not None:
                        message += f" Current widest layer: {widest_layer}."
                if self._spawn_validation_failures >= 2:
                    message += (
                        " Stop retrying the same malformed payload. Rebuild the JSON "
                        "carefully before calling the tool again."
                    )
                return ToolResponse(
                    content=[TextBlock(type="text", text=message)],
                )

            self._spawn_validation_failures = 0
            self._proposal = {
                "subtasks": deepcopy(subtasks),
                "dependencies": deepcopy(dep_list),
            }
            self._spawn_allowed = False
            if callable(self._on_proposal_captured):
                try:
                    self._on_proposal_captured(deepcopy(self._proposal))
                except Exception as callback_error:
                    logger.warning(
                        "[ShadowClone] Proposal-captured callback failed run_id=%s: %s",
                        self.agent_run_id,
                        callback_error,
                        exc_info=True,
                    )
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "Shadow Clone proposal captured successfully. "
                            "Stop here. The runner will now request user "
                            "confirmation and execute the plan."
                        ),
                    ),
                ],
            )

        def resolve_failed_subtasks(
            action: str,
            subtask_ids: list[str] | None = None,
            rationale: str | None = None,
        ) -> ToolResponse:
            normalized_action = str(action or "").strip().lower()
            if normalized_action not in {
                "retry_failed_subtasks",
                "continue_with_partial_results",
            }:
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                "Error: `action` must be either "
                                "`retry_failed_subtasks` or `continue_with_partial_results`."
                            ),
                        ),
                    ],
                )

            normalized_ids = [
                str(item).strip()
                for item in (subtask_ids or [])
                if str(item).strip()
            ]
            if normalized_action == "retry_failed_subtasks" and not normalized_ids:
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                "Error: `retry_failed_subtasks` requires a non-empty `subtask_ids` array."
                            ),
                        ),
                    ],
                )

            self._layer_recovery_decision = {
                "action": normalized_action,
                "subtask_ids": normalized_ids,
                "rationale": str(rationale or "").strip() or None,
            }
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            "Failed-layer recovery decision captured. Stop here so the coordinator can apply it."
                        ),
                    ),
                ],
            )

        async def read_results() -> ToolResponse:
            rows = await read_summaries(self.agent_run_id)
            compact_rows = self._build_compact_subtask_result_rows(rows)
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=json.dumps(
                            compact_rows,
                            ensure_ascii=False,
                            default=str,
                            indent=2,
                        ),
                    ),
                ],
            )

        async def read_full_result(subtask_id: str) -> ToolResponse:
            text = await read_full_result_entry(self.agent_run_id, subtask_id)
            if text is None:
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                f"No result found for subtask '{subtask_id}'. "
                                "It may have expired or never completed."
                            ),
                        ),
                    ],
                )

            max_chars = int(os.getenv("AGENTSCOPE_TOOL_OUTPUT_MAX_CHARS", "12000"))
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n... [result truncated]"

            return ToolResponse(
                content=[TextBlock(type="text", text=text)],
            )

        parallel_tool_calls_enabled = _env_flag(
            "AGENTSCOPE_PARALLEL_TOOL_CALLS",
            True,
        )

        long_term_memory = None
        if self._ltm_settings.enabled:
            logger.info(
                "[ShadowClone] MainAgent LTM bootstrap start run_id=%s config_path=%s backend_hint=%s",
                self.agent_run_id,
                self._ltm_settings.reme_config_path or "<default>",
                self._ltm_backend_hint(),
            )
            if self._ltm_settings.attach_scope != "orchestrator":
                logger.info(
                    "[ShadowClone] MainAgent LTM enabled but attach_scope=%s is unsupported for Shadow Clone parity. "
                    "Proceeding without LTM attachment.",
                    self._ltm_settings.attach_scope,
                )
            else:
                try:
                    self._long_term_memory = create_long_term_memory(
                        thread_id=self.thread_id,
                        settings=self._ltm_settings,
                    )
                    if self._long_term_memory is not None:
                        await self._long_term_memory.__aenter__()
                        self._ltm_entered = True
                        long_term_memory = self._long_term_memory
                        logger.info(
                            "[ShadowClone] MainAgent LTM sidecar attached run_id=%s mode=%s memories=%s config_path=%s backend_hint=%s",
                            self.agent_run_id,
                            self._ltm_settings.control_mode,
                            ",".join(self._ltm_settings.memories),
                            self._ltm_settings.reme_config_path or "<default>",
                            self._ltm_backend_hint(),
                        )
                    else:
                        logger.info(
                            "[ShadowClone] MainAgent LTM enabled but no memories were created from current config run_id=%s",
                            self.agent_run_id,
                        )
                except Exception as exc:
                    if self._ltm_settings.fail_open:
                        logger.warning(
                            "[ShadowClone] MainAgent LTM initialization failed in fail-open mode run_id=%s: %s "
                            "(config_path=%s backend_hint=%s)",
                            self.agent_run_id,
                            exc,
                            self._ltm_settings.reme_config_path or "<default>",
                            self._ltm_backend_hint(),
                        )
                        self._long_term_memory = None
                        self._ltm_entered = False
                    else:
                        await self._close_long_term_memory()
                        raise


        shared_toolkit_adapter = ToolkitAdapter(
            project_id=self.project_id,
            thread_id=self.thread_id,
            thread_manager=thread_manager,
            db_client=self.db_client,
            shadow_clone_run_id=self.agent_run_id,
            strict_sandbox=True,
            shadow_clone_fail_fast=False,
            trace=self._lf_trace,
        )
        self._shared_toolkit_adapter = shared_toolkit_adapter

        def _build_toolkit(
            *,
            include_spawn: bool,
            include_result_reads: bool,
            include_failure_review: bool = False,
        ):
            if hasattr(shared_toolkit_adapter, "clone_toolkit"):
                toolkit = shared_toolkit_adapter.clone_toolkit()
            else:
                toolkit_adapter = ToolkitAdapter(
                    project_id=self.project_id,
                    thread_id=self.thread_id,
                    thread_manager=thread_manager,
                    db_client=self.db_client,
                    shadow_clone_run_id=self.agent_run_id,
                    strict_sandbox=True,
                    shadow_clone_fail_fast=False,
                    trace=self._lf_trace,
                )
                toolkit = toolkit_adapter.get_toolkit()
            if include_spawn:
                toolkit.register_tool_function(
                    spawn_subagents,
                    json_schema=_spawn_subagents_json_schema(),
                )
            if include_result_reads:
                toolkit.register_tool_function(read_results)
                toolkit.register_tool_function(read_full_result)
            if include_failure_review:
                toolkit.register_tool_function(resolve_failed_subtasks)
            return toolkit

        def _create_agent(
            sys_prompt: str,
            toolkit,
            *,
            phase_memory: MessagesTableMemory,
        ) -> ReActAgent:
            agent = ReActAgent(
                name="ShadowClone-MainAgent",
                sys_prompt=sys_prompt,
                model=model,
                formatter=formatter,
                toolkit=toolkit,
                memory=phase_memory,
                long_term_memory=long_term_memory,
                long_term_memory_mode=self._ltm_settings.control_mode,
                max_iters=120,
                parallel_tool_calls=parallel_tool_calls_enabled,
            )
            self._register_workspace_guard(agent)
            self._register_micro_compact(agent, phase_memory)
            return agent

        planning_agent = _create_agent(
            self._build_decompose_prompt(),
            _build_toolkit(include_spawn=True, include_result_reads=False),
            phase_memory=memory,
        )
        direct_agent = _create_agent(
            self._build_denial_prompt(),
            _build_toolkit(include_spawn=False, include_result_reads=False),
            phase_memory=memory,
        )
        aggregate_agent = _create_agent(
            self._build_aggregate_prompt(),
            _build_toolkit(include_spawn=False, include_result_reads=True),
            phase_memory=aggregate_memory,
        )
        recovery_agent = _create_agent(
            self._build_recovery_prompt(),
            _build_toolkit(include_spawn=False, include_result_reads=True),
            phase_memory=aggregate_memory,
        )
        failure_review_agent = _create_agent(
            self._build_failure_review_prompt(),
            _build_toolkit(
                include_spawn=False,
                include_result_reads=True,
                include_failure_review=True,
            ),
            phase_memory=aggregate_memory,
        )

        self._memory = memory
        self._main_agent = planning_agent
        self._decompose_agent = planning_agent
        self._direct_agent = direct_agent
        self._aggregate_agent = aggregate_agent
        self._recovery_agent = recovery_agent
        self._failure_review_agent = failure_review_agent
        self._initialized = True

    async def prepare_shadow_clone_environment_commit(
        self,
        *,
        execution_epoch: int,
    ) -> Dict[str, Any]:
        from agent.tools.sandbox_skill_tool import (
            BOOTSTRAP_METADATA,
            BOOTSTRAP_MODE,
            BOOTSTRAP_TARGET,
            SKILLS_WORKSPACE_DIR,
        )
        from agentscope_integration.shadow_clone.sandbox_lease import get_run_sandbox_lease
        from sandbox.tool_base import SandboxToolsBase

        await self.setup()
        toolkit_adapter = self._shared_toolkit_adapter
        if toolkit_adapter is None:
            raise RuntimeError("Shadow Clone shared toolkit adapter was not initialized")

        skill_tool = toolkit_adapter.get_tool_instance("skill")
        if skill_tool is None:
            raise RuntimeError("Shadow Clone shared skill tool is unavailable")

        metadata = await skill_tool._load_metadata()
        refreshed_lease = await get_run_sandbox_lease(self.agent_run_id) or {}
        sandbox_info = skill_tool._parse_sandbox_info(refreshed_lease.get("sandbox_info"))
        sandbox_type = str(
            refreshed_lease.get("sandbox_type")
            or sandbox_info.get("type")
            or skill_tool.sandbox_type
            or "desktop"
        ).strip() or "desktop"
        template_lineage = SandboxToolsBase._build_template_lineage(
            sandbox_type,
            sandbox_obj=skill_tool._get_runtime_sandbox(),
            fallback_template_id=SandboxToolsBase._normalize_template_value(
                sandbox_info.get("template_id")
            ),
        )
        prepared_at = datetime.now(timezone.utc).isoformat()
        runtime_sandbox_id = str(
            skill_tool._get_runtime_sandbox_id() or refreshed_lease.get("sandbox_id") or ""
        ).strip()

        return {
            "prepared_by": "shadow_clone_main_agent_shared_toolkit",
            "environment_contract": "run_scoped_environment_commit",
            "environment_commit_id": (
                f"{self.agent_run_id}:env:{max(0, int(execution_epoch or 0))}:{uuid.uuid4().hex[:12]}"
            ),
            "prepared_at": prepared_at,
            "execution_epoch": max(0, int(execution_epoch or 0)),
            "sandbox": {
                "id": runtime_sandbox_id,
                "type": sandbox_type,
                "binding_state": str(refreshed_lease.get("binding_state") or ""),
                **template_lineage,
            },
            "bootstrap": {
                "status": "ready",
                "mode": BOOTSTRAP_MODE,
                "target": BOOTSTRAP_TARGET,
                "metadata_path": BOOTSTRAP_METADATA,
            },
            "skills": {
                "ready": True,
                "count": len((metadata or {}).get("skills") or []),
                "workspace_dir": SKILLS_WORKSPACE_DIR,
            },
        }

    async def close(self) -> None:
        await self._close_long_term_memory()

    async def _stream_turn(
        self,
        user_message: str,
        *,
        agent: ReActAgent,
        allow_spawn: bool,
        store_attr: str,
    ) -> AsyncGenerator[tuple[Any, bool], None]:
        await self.setup()
        assert agent is not None

        self._proposal = None
        self._spawn_validation_failures = 0
        self._spawn_allowed = allow_spawn

        request_message = Msg(name="user", content=user_message, role="user")
        final_response = None
        last_visible_message = None
        emitted_visible_complete = False
        queue: asyncio.Queue = asyncio.Queue()
        proposal_cancelled = False

        self._disable_console_output(agent)
        agent.set_msg_queue_enabled(True, queue)

        reply_callable = getattr(agent, "reply", None)
        if not callable(reply_callable):
            agent.set_msg_queue_enabled(False)
            raise AttributeError("Shadow Clone main agent requires a reply() method")

        if hasattr(agent, "_reply_id"):
            try:
                agent._reply_id = uuid.uuid4().hex
            except Exception:
                pass

        reply_task = asyncio.create_task(reply_callable(request_message))
        if reply_task.done():
            queue.put_nowait(_TURN_STREAM_END)
        else:
            reply_task.add_done_callback(lambda _: queue.put_nowait(_TURN_STREAM_END))

        try:
            while True:
                queue_item = await queue.get()
                if queue_item is _TURN_STREAM_END:
                    break

                try:
                    msg, is_last, _speech = queue_item
                except Exception:
                    continue

                if (
                    allow_spawn
                    and self._proposal is not None
                    and not proposal_cancelled
                    and not reply_task.done()
                ):
                    proposal_cancelled = True
                    reply_task.cancel()

                if self._is_internal_spawn_subagents_message(msg):
                    continue

                if is_last:
                    final_response = msg
                last_visible_message = msg
                if is_last:
                    emitted_visible_complete = True
                yield msg, is_last

            try:
                response = await reply_task
            except asyncio.CancelledError:
                if not (proposal_cancelled and self._proposal is not None):
                    raise
                response = None

            if response is not None:
                final_response = final_response or response
                if (
                    last_visible_message is None
                    and not self._is_internal_spawn_subagents_message(response)
                ):
                    last_visible_message = response
                    emitted_visible_complete = True
        finally:
            agent.set_msg_queue_enabled(False)
            self._spawn_allowed = True

        setattr(
            self,
            store_attr,
            _msg_to_text(final_response or last_visible_message),
        )
        if self._proposal:
            logger.info(
                "[ShadowClone] MainAgent produced proposal (run_id=%s subtasks=%s)",
                self.agent_run_id,
                len(self._proposal.get("subtasks", [])),
            )
            if last_visible_message is not None and not emitted_visible_complete:
                yield last_visible_message, True

    async def _run_turn_once(
        self,
        user_message: str,
        *,
        agent: ReActAgent,
        allow_spawn: bool,
        store_attr: str,
    ) -> None:
        await self.setup()
        assert agent is not None

        self._proposal = None
        self._spawn_validation_failures = 0
        self._spawn_allowed = allow_spawn
        queue: asyncio.Queue = asyncio.Queue()
        proposal_cancelled = False
        try:
            self._disable_console_output(agent)
            agent.set_msg_queue_enabled(True, queue)

            reply_callable = getattr(agent, "reply", None)
            if not callable(reply_callable):
                raise AttributeError("Shadow Clone main agent requires a reply() method")

            if hasattr(agent, "_reply_id"):
                try:
                    agent._reply_id = uuid.uuid4().hex
                except Exception:
                    pass

            request_message = Msg(name="user", content=user_message, role="user")
            reply_task = asyncio.create_task(reply_callable(request_message))
            if reply_task.done():
                queue.put_nowait(_TURN_STREAM_END)
            else:
                reply_task.add_done_callback(lambda _: queue.put_nowait(_TURN_STREAM_END))

            final_response = None
            last_visible_message = None
            while True:
                queue_item = await queue.get()
                if queue_item is _TURN_STREAM_END:
                    break

                try:
                    msg, is_last, _speech = queue_item
                except Exception:
                    continue

                if (
                    allow_spawn
                    and self._proposal is not None
                    and not proposal_cancelled
                    and not reply_task.done()
                ):
                    proposal_cancelled = True
                    reply_task.cancel()

                if self._is_internal_spawn_subagents_message(msg):
                    continue

                last_visible_message = msg
                if is_last:
                    final_response = msg

            try:
                response = await reply_task
            except asyncio.CancelledError:
                if not (proposal_cancelled and self._proposal is not None):
                    raise
                response = None

            if response is not None:
                final_response = final_response or response
                if (
                    last_visible_message is None
                    and not self._is_internal_spawn_subagents_message(response)
                ):
                    last_visible_message = response

            setattr(
                self,
                store_attr,
                _msg_to_text(final_response or last_visible_message),
            )
        finally:
            agent.set_msg_queue_enabled(False)
            self._spawn_allowed = True

    async def stream_decompose(
        self,
        user_message: str,
    ) -> AsyncGenerator[tuple[Any, bool], None]:
        await self.setup()
        assert self._decompose_agent is not None
        async for item in self._stream_turn(
            user_message,
            agent=self._decompose_agent,
            allow_spawn=True,
            store_attr="_last_response_text",
        ):
            yield item

    async def stream_after_denial(self) -> AsyncGenerator[tuple[Any, bool], None]:
        await self.setup()
        assert self._direct_agent is not None
        async for item in self._stream_turn(
            self._build_denial_message(),
            agent=self._direct_agent,
            allow_spawn=False,
            store_attr="_last_response_text",
        ):
            yield item

    async def decompose(self, user_message: str) -> Optional[Dict[str, Any]]:
        await self.setup()
        assert self._decompose_agent is not None
        await self._run_turn_once(
            user_message,
            agent=self._decompose_agent,
            allow_spawn=True,
            store_attr="_last_response_text",
        )
        if self._proposal:
            logger.info(
                "[ShadowClone] MainAgent produced proposal (run_id=%s subtasks=%s)",
                self.agent_run_id,
                len(self._proposal.get("subtasks", [])),
            )
            return deepcopy(self._proposal)
        return None

    async def after_denial(self) -> str:
        await self.setup()
        assert self._direct_agent is not None
        await self._run_turn_once(
            self._build_denial_message(),
            agent=self._direct_agent,
            allow_spawn=False,
            store_attr="_last_response_text",
        )
        return self._last_response_text

    async def aggregate(self) -> str:
        await self.setup()
        assert self._aggregate_agent is not None
        rows = await read_summaries(self.agent_run_id)
        logger.info(
            "[ShadowClone] Aggregate continuation starting run_id=%s summary_rows=%s",
            self.agent_run_id,
            len(rows),
        )
        if not rows:
            logger.warning(
                "[ShadowClone] No subagent summaries found for post-subtask continuation (run_id=%s)",
                self.agent_run_id,
            )
            return "No subagent results available for continuation."

        await self._run_turn_once(
            self._build_subtask_results_message(rows),
            agent=self._aggregate_agent,
            allow_spawn=False,
            store_attr="_last_aggregate_text",
        )
        return self._last_aggregate_text

    async def stream_aggregate(self) -> AsyncGenerator[tuple[Any, bool], None]:
        await self.setup()
        assert self._aggregate_agent is not None
        rows = await read_summaries(self.agent_run_id)
        logger.info(
            "[ShadowClone] Aggregate stream starting run_id=%s summary_rows=%s",
            self.agent_run_id,
            len(rows),
        )
        if not rows:
            logger.warning(
                "[ShadowClone] No subagent summaries found for post-subtask continuation (run_id=%s)",
                self.agent_run_id,
            )
            fallback_message = Msg(
                name="assistant",
                content="No subagent results available for continuation.",
                role="assistant",
            )
            self._last_aggregate_text = _msg_to_text(fallback_message)
            yield fallback_message, True
            return

        async for item in self._stream_turn(
            self._build_subtask_results_message(rows),
            agent=self._aggregate_agent,
            allow_spawn=False,
            store_attr="_last_aggregate_text",
        ):
            yield item

    async def stream_after_sandbox_failure(
        self,
        recovery_message: str,
    ) -> AsyncGenerator[tuple[Any, bool], None]:
        await self.setup()
        assert self._recovery_agent is not None
        async for item in self._stream_turn(
            recovery_message,
            agent=self._recovery_agent,
            allow_spawn=False,
            store_attr="_last_response_text",
        ):
            yield item

    async def review_failed_layer(
        self,
        review_message: str,
    ) -> Dict[str, Any]:
        await self.setup()
        assert self._failure_review_agent is not None
        self._layer_recovery_decision = None
        await self._run_turn_once(
            review_message,
            agent=self._failure_review_agent,
            allow_spawn=False,
            store_attr="_last_response_text",
        )
        if self._layer_recovery_decision is None:
            return {
                "action": "continue_with_partial_results",
                "subtask_ids": [],
                "rationale": "No explicit failed-layer recovery decision was returned.",
            }
        return deepcopy(self._layer_recovery_decision)

    def get_proposal(self) -> Optional[Dict[str, Any]]:
        if self._proposal is None:
            return None
        return deepcopy(self._proposal)

    def get_last_response_text(self) -> str:
        return self._last_response_text

    def get_last_aggregate_text(self) -> str:
        return self._last_aggregate_text
