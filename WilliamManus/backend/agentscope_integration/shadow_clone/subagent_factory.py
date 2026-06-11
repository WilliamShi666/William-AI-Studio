"""Factory for building Shadow Clone subagent instances."""

from __future__ import annotations

import os
from typing import Any, Dict

from agentscope.message import Msg, TextBlock
from agentscope.tool import ToolResponse

from agentscope_integration.agents.metadata_aware_react_agent import (
    MetadataAwareReActAgent as ReActAgent,
)
from agentscope_integration.adapters.thread_manager_adapter import ThreadManagerAdapter
from agentscope_integration.context_builder import ContextBuilder
from agentscope_integration.memory import (
    MessagesTableMemory,
    ReadOnlyDelegateLongTermMemory,
    create_long_term_memory,
    load_ltm_settings_from_env,
)
from agentscope_integration.models import ModelFactory
from agentscope_integration.prompts.worker_prompt import get_worker_prompt
from agentscope_integration.tools import ToolkitAdapter
from utils.logger import logger

from .constants import (
    SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
    SHADOW_CLONE_SUBAGENT_MODEL,
)
from .peer_mailbox import (
    PEER_NOTE_MEMORY_MARK,
    append_peer_note,
    collect_seen_note_ids,
    drain_new_notes,
    format_notes_for_injection,
)
from .result_store import read_full_result as read_full_result_entry
from .state_machine import get_state

SHADOW_CLONE_SUBAGENT_DELTA = """\

## Shadow Clone SubAgent Overlay
You are operating as a Shadow Clone subagent.
- You share the same baseline execution rules as the normal Worker prompt.
- Your assigned role and task are defined below.
- Stay tightly scoped to your assigned task.
- Use direct tools to complete the task; do not ask whether you should proceed.
- Treat files created by other subtasks as incidental hints only unless your task explicitly depends on them.
- If the environment reports that the bound sandbox cannot be reattached or recovered, stop retrying sandbox-dependent actions.
- When sandbox access is lost, preserve any conclusions you already reached and rely on earlier-layer results or durable artifacts when possible.
- **WORKSPACE FILE COLLECTION (MANDATORY — FINAL STEP):**
  Before you finish, ensure ALL files and folders you produced are under `/workspace`:
  1. If you created files outside `/workspace` (e.g. in `/tmp`, `/home`, `/root`, `/var`, `/opt`), copy them:
     `cp -R /path/to/output /workspace/` (for directories) or `cp /path/to/file /workspace/` (for files).
  2. If you are unsure whether any output landed outside `/workspace`, run:
     `find /tmp /home /root -maxdepth 3 -newer /proc/1/cmdline -type f 2>/dev/null`
     and copy any task-relevant results into `/workspace`.
  3. In your final answer, reference ONLY `/workspace/...` paths — never external paths.
  Users can ONLY access files under `/workspace`. Files left outside are invisible and lost.
"""


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _should_use_full_model_name_resolution(model_name: str) -> bool:
    normalized = str(model_name or "").strip()
    return "/" in normalized


def _build_subagent_prompt(subtask_config: Dict[str, Any]) -> str:
    role = str(subtask_config.get("role") or "specialist").strip()
    task_description = str(subtask_config.get("task_description") or "").strip()
    system_prompt = str(subtask_config.get("system_prompt") or "").strip()
    recovery_mode = str(subtask_config.get("shadow_recovery_mode") or "").strip()
    recovery_reason = str(subtask_config.get("shadow_recovery_reason") or "").strip()
    recovery_handoff_summary = str(
        subtask_config.get("shadow_recovery_handoff_summary") or "",
    ).strip()

    layer_index = int(subtask_config.get("layer_index") or 0)
    shared_workspace_hint = (
        "IMPORTANT — SHARED WORKSPACE AWARENESS: Shadow Clone subagents in the same run "
        "may reuse the same underlying sandbox. You may see files in /workspace created by "
        "other subtasks. Treat those files as incidental hints only — not authoritative task inputs. "
        "Same-layer subtasks can run concurrently, so do not assume shared /workspace files are "
        "complete, current, or intended for you. "
        "Regardless of what other subtasks do, YOUR output files MUST all end up under /workspace."
    )
    sections = [
        get_worker_prompt(),
        SHADOW_CLONE_SUBAGENT_DELTA,
        f"Assigned role: {role}",
        "Assigned task:\n" + task_description,
        shared_workspace_hint,
        (
            "PEER NOTES: Use `send_peer_note(recipient_subtask_id, summary, details, kind)` "
            "only for high-value findings that can help another subagent. "
            "If peer notes arrive for you, treat them as hints to verify, not authoritative facts."
        ),
        (
            "LONG-TERM MEMORY: If auto-retrieved long-term memory context is present, use it first. "
            "If `retrieve_from_memory` is available and the current context is still insufficient, "
            "use it for cross-run task/tool experience. Do not treat it as a substitute for same-run "
            "artifacts produced by earlier Shadow Clone subtasks."
        ),
    ]
    if layer_index > 0:
        sections.append(
            "IMPORTANT — DETERMINISTIC INPUTS: If your task requires files or content "
            "produced by earlier-layer subtasks, use the `read_full_result(subtask_id)` tool "
            "to retrieve their output, then recreate or verify any required files before "
            "relying on them.\n\n"
            "You also have access to `read_full_result` for reading detailed results "
            "from earlier-layer subtasks when summaries are insufficient."
        )
    if recovery_mode == "wake_first":
        sections.append(
            "RUNTIME RECOVERY: This subtask is being resumed after an interrupted or failed attempt. "
            "Continue from the existing context already stored in memory. Do not restart the task from "
            "scratch unless the existing context is unusable."
        )
    if recovery_mode == "replacement" and recovery_handoff_summary:
        sections.append(
            "RUNTIME HANDOFF: You are taking over this subtask after an earlier attempt could not "
            "continue safely. Use the handoff below to continue the work rather than restarting it.\n\n"
            + recovery_handoff_summary
        )
    elif recovery_handoff_summary:
        sections.append(
            "RUNTIME CONTEXT:\n" + recovery_handoff_summary
        )
    if recovery_reason:
        sections.append(
            "Recovery trigger:\n" + recovery_reason
        )
    if system_prompt:
        sections.append(
            "Task-specific extra instructions (additive only; shared baseline rules still apply):\n"
            + system_prompt,
        )

    return "\n\n".join(section for section in sections if section)


async def _create_readonly_delegate_ltm(thread_id: str):
    ltm_settings = load_ltm_settings_from_env()
    if not ltm_settings.enabled:
        return None

    if ltm_settings.attach_scope != "orchestrator":
        logger.info(
            "[ShadowClone] SubAgent LTM enabled but attach_scope=%s is unsupported for delegate retrieval parity. "
            "Proceeding without delegate LTM attachment.",
            ltm_settings.attach_scope,
        )
        return None

    try:
        delegate = create_long_term_memory(
            thread_id=thread_id,
            settings=ltm_settings,
        )
        if delegate is None:
            logger.info(
                "[ShadowClone] SubAgent delegate LTM enabled but no memories were created for thread_id=%s",
                thread_id,
            )
            return None

        readonly_ltm = ReadOnlyDelegateLongTermMemory(
            delegate,
            manage_lifecycle=True,
        )
        await readonly_ltm.__aenter__()
        return readonly_ltm
    except Exception as exc:
        if ltm_settings.fail_open:
            logger.warning(
                "[ShadowClone] SubAgent delegate LTM initialization failed in fail-open mode thread_id=%s: %s",
                thread_id,
                exc,
            )
            return None
        raise


async def _load_allowed_peer_recipients(parent_run_id: str, subtask_id: str) -> Dict[str, str]:
    try:
        state = await get_state(
            parent_run_id,
            timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
        )
    except Exception:
        logger.warning(
            "[ShadowClone] Failed to load peer recipient state run_id=%s subtask_id=%s",
            parent_run_id,
            subtask_id,
            exc_info=True,
        )
        return {}

    subagents = state.get("subagents") if isinstance(state, dict) else {}
    if not isinstance(subagents, dict):
        return {}

    recipients: Dict[str, str] = {}
    for candidate_id, payload in subagents.items():
        candidate = str(candidate_id or "").strip()
        if not candidate or candidate == str(subtask_id):
            continue
        role = ""
        if isinstance(payload, dict):
            role = str(payload.get("role") or "").strip()
        recipients[candidate] = role
    return recipients


async def create_subagent(
    subtask_config: Dict[str, Any],
    project_id: str,
    parent_run_id: str,
    subtask_id: str,
    db_client,
    *,
    thread_id: str,
    max_iters: int = 500,
    langfuse_trace_id: str = "",
    langfuse_parent_observation: Any | None = None,
) -> ReActAgent:
    """
    Build one ReAct SubAgent instance with isolated persistent memory.
    """
    _lf_trace = langfuse_parent_observation
    if _lf_trace is None and langfuse_trace_id:
        try:
            from services.langfuse import langfuse as _lf_client

            _lf_trace = _lf_client.start_observation(
                name="subagent_root",
                as_type="span",
                trace_context={"trace_id": langfuse_trace_id},
            )
        except Exception:
            pass

    model_key = str(
        subtask_config.get("model") or SHADOW_CLONE_SUBAGENT_MODEL,
    ).strip()
    if _should_use_full_model_name_resolution(model_key):
        model, formatter = ModelFactory.create_from_full_name(
            model_name=model_key,
            trace=_lf_trace,
        )
    else:
        model, formatter = ModelFactory.create(
            model_key=model_key,
            trace=_lf_trace,
        )
    context_builder = ContextBuilder.from_env()

    thread_run_id = str(
        subtask_config.get("shadow_thread_run_id")
        or f"shadow:{parent_run_id}:{subtask_id}",
    ).strip()
    memory = MessagesTableMemory(
        db_client=db_client,
        **context_builder.build_worker_memory_kwargs(
            thread_id=thread_id,
            project_id=project_id,
            thread_run_id=thread_run_id,
            model_key=model_key,
        ),
    )
    toolkit_adapter = ToolkitAdapter(
        project_id=project_id,
        thread_id=thread_id,
        thread_manager=ThreadManagerAdapter(db_client=db_client),
        db_client=db_client,
        shadow_clone_run_id=parent_run_id,
        strict_sandbox=True,
        shadow_clone_fail_fast=True,
        trace=_lf_trace,
    )

    toolkit = toolkit_adapter.get_toolkit()
    long_term_memory = await _create_readonly_delegate_ltm(thread_id)
    if long_term_memory is not None:
        toolkit.register_tool_function(long_term_memory.retrieve_from_memory)
    sender_role = str(subtask_config.get("role") or "specialist").strip()
    recipient_role_map = await _load_allowed_peer_recipients(parent_run_id, subtask_id)
    allowed_recipients = frozenset(recipient_role_map.keys())

    async def send_peer_note(
        recipient_subtask_id: str,
        summary: str,
        details: str = "",
        kind: str = "cross_task_hint",
    ) -> ToolResponse:
        """
        Send a concise cross-task hint to another Shadow Clone subagent.

        Use this only for findings that are likely to materially help the recipient.
        """
        recipient = str(recipient_subtask_id or "").strip()
        if not recipient:
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text="Peer note not sent: recipient_subtask_id must be non-empty.",
                    ),
                ],
            )
        if recipient not in allowed_recipients:
            available = ", ".join(sorted(allowed_recipients)) or "none"
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=(
                            f"Peer note not sent: '{recipient}' is not an allowed peer recipient. "
                            f"Available recipients: {available}."
                        ),
                    ),
                ],
            )

        try:
            await append_peer_note(
                run_id=parent_run_id,
                sender_subtask_id=subtask_id,
                sender_role=sender_role,
                recipient_subtask_id=recipient,
                recipient_role=recipient_role_map.get(recipient, ""),
                summary=summary,
                details=details,
                kind=kind,
                timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning(
                "[ShadowClone] Failed to queue peer note run_id=%s sender=%s recipient=%s error=%s",
                parent_run_id,
                subtask_id,
                recipient,
                exc,
                exc_info=True,
            )
            return ToolResponse(
                content=[
                    TextBlock(
                        type="text",
                        text=f"Peer note failed for subtask '{recipient}': {exc}",
                    ),
                ],
            )

        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=f"Peer note queued for subtask '{recipient}'.",
                ),
            ],
        )

    toolkit.register_tool_function(send_peer_note)

    # Inject read_full_result tool for layer > 0 subagents
    layer_index = int(subtask_config.get("layer_index") or 0)
    if layer_index > 0:
        async def read_full_result(subtask_id: str) -> ToolResponse:
            """
            Read the full result text for a specific subtask by its ID.

            Use this to access complete results from earlier-layer subtasks
            when summaries are insufficient for your analysis.
            """
            from agentscope.message import TextBlock

            text = await read_full_result_entry(parent_run_id, subtask_id)
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

        toolkit.register_tool_function(read_full_result)

    parallel_tool_calls_enabled = _env_flag(
        "AGENTSCOPE_PARALLEL_TOOL_CALLS",
        True,
    )
    agent = ReActAgent(
        name=f"SubAgent-{subtask_id}",
        sys_prompt=_build_subagent_prompt(subtask_config),
        model=model,
        formatter=formatter,
        toolkit=toolkit,
        memory=memory,
        long_term_memory=long_term_memory,
        long_term_memory_mode="static_control",
        max_iters=max_iters,
        parallel_tool_calls=parallel_tool_calls_enabled,
    )

    async def _stage_peer_notes(hooked_agent, kwargs):
        try:
            cursor = int(getattr(hooked_agent, "_shadow_peer_note_cursor", 0) or 0)
            seen_ids = getattr(hooked_agent, "_shadow_seen_peer_note_ids", set())
            staged_notes = list(getattr(hooked_agent, "_shadow_pending_peer_notes", []))
            new_notes, next_cursor = await drain_new_notes(
                run_id=parent_run_id,
                recipient_subtask_id=subtask_id,
                cursor=cursor,
                seen_note_ids=seen_ids,
                timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
            )
            if new_notes:
                staged_notes.extend(new_notes)
                setattr(hooked_agent, "_shadow_pending_peer_notes", staged_notes)
            setattr(hooked_agent, "_shadow_peer_note_cursor", next_cursor)
        except Exception:
            logger.debug(
                "[ShadowClone] Peer note staging failed run_id=%s subtask_id=%s",
                parent_run_id,
                subtask_id,
                exc_info=True,
            )
        return None

    async def _inject_peer_notes(hooked_agent, kwargs):
        try:
            cursor = int(getattr(hooked_agent, "_shadow_peer_note_cursor", 0) or 0)
            seen_ids = set(getattr(hooked_agent, "_shadow_seen_peer_note_ids", set()) or set())
            pending_notes = list(getattr(hooked_agent, "_shadow_pending_peer_notes", []))
            new_notes, next_cursor = await drain_new_notes(
                run_id=parent_run_id,
                recipient_subtask_id=subtask_id,
                cursor=cursor,
                seen_note_ids=seen_ids,
                timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
            )
            if new_notes:
                pending_notes.extend(new_notes)
            setattr(hooked_agent, "_shadow_peer_note_cursor", next_cursor)
            if not pending_notes:
                return None

            injection_msg = Msg(
                "user",
                format_notes_for_injection(
                    pending_notes,
                    recipient_subtask_id=subtask_id,
                ),
                "user",
            )
            await hooked_agent.memory.add(
                injection_msg,
                marks=PEER_NOTE_MEMORY_MARK,
            )
            setattr(
                hooked_agent,
                "_shadow_seen_peer_note_ids",
                collect_seen_note_ids(seen_ids, pending_notes),
            )
            setattr(hooked_agent, "_shadow_pending_peer_notes", [])
        except Exception:
            logger.debug(
                "[ShadowClone] Peer note injection failed run_id=%s subtask_id=%s",
                parent_run_id,
                subtask_id,
                exc_info=True,
            )
        return None

    agent.register_instance_hook(
        hook_type="pre_reply",
        hook_name="shadow_peer_note_prefetch",
        hook=_stage_peer_notes,
    )
    agent.register_instance_hook(
        hook_type="pre_reasoning",
        hook_name="shadow_peer_note_inject",
        hook=_inject_peer_notes,
    )

    # Micro Compact: prune old tool outputs for sub-agents
    from ..memory.micro_compact import make_micro_compact_hook
    mc_enabled = getattr(memory, 'enable_micro_compact', True)
    agent.register_instance_hook(
        hook_type="pre_reasoning",
        hook_name="micro_compact",
        hook=make_micro_compact_hook(enabled=mc_enabled),
    )

    # Keep references attached for debugging/testing.
    setattr(agent, "_shadow_toolkit_adapter", toolkit_adapter)
    setattr(agent, "_shadow_memory", memory)
    setattr(agent, "_shadow_thread_run_id", thread_run_id)
    setattr(agent, "_shadow_readonly_long_term_memory", long_term_memory)
    setattr(agent, "_shadow_parent_run_id", parent_run_id)
    setattr(agent, "_shadow_subtask_id", subtask_id)
    setattr(agent, "_shadow_role", sender_role)
    setattr(agent, "_shadow_allowed_peer_recipients", allowed_recipients)
    setattr(agent, "_shadow_peer_recipient_roles", dict(recipient_role_map))
    setattr(agent, "_shadow_peer_note_cursor", 0)
    setattr(agent, "_shadow_seen_peer_note_ids", set())
    setattr(agent, "_shadow_pending_peer_notes", [])

    logger.info(
        "[ShadowClone] Created SubAgent subtask_id=%s role=%s model=%s thread_run_id=%s",
        subtask_id,
        subtask_config.get("role"),
        model_key,
        thread_run_id,
    )
    return agent
