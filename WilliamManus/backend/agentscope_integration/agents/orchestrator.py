"""
Orchestrator Agent Implementation (Optimized Version)

The Orchestrator agent is responsible for:
- Executing most tasks directly using its full toolkit
- Delegating ONLY deep research tasks to the Worker agent
- Coordinating multi-step workflows and iterating until completion

Key changes from previous version:
- Orchestrator now has access to ALL tools (not just delegate_to_worker)
- Delegation is limited to deep research tasks only
- Added support for memory compression configuration
"""

from typing import Optional
from datetime import datetime, timezone
import os
import re
import json

from agentscope.tool import Toolkit, ToolResponse
from agentscope.token import CharTokenCounter
from agentscope.message import Msg, TextBlock

from .metadata_aware_react_agent import MetadataAwareReActAgent as ReActAgent
from ..prompts.orchestrator_prompt import get_orchestrator_prompt
from ..utils.workspace_guard import relocate_external_paths
from utils.logger import logger
from utils.agent_run_context import get_agent_run_context
from ..state import ResumeCoordinator, ResumeState


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class OrchestratorAgent:
    """
    Orchestrator Agent - Executes most tasks directly, delegates only deep research.
    
    The Orchestrator has access to ALL tools and can:
    - Execute most tasks directly using its toolkit
    - Delegate deep research tasks to the Worker agent
    """
    
    def __init__(
        self,
        model,
        formatter,
        worker,
        full_toolkit: Optional[Toolkit] = None,
        memory=None,
        long_term_memory=None,
        long_term_memory_mode: str = "both",
        max_iters: int = 500,
        compression_config=None,
        resume_coordinator: Optional[ResumeCoordinator] = None,
    ):
        """
        Initialize the Orchestrator agent.
        
        Args:
            model: AgentScope model instance
            formatter: AgentScope formatter instance
            worker: Worker agent instance
            full_toolkit: Complete toolkit with all tools (Orchestrator will use these directly)
            memory: Memory instance (optional)
            long_term_memory: Long-term memory sidecar (optional)
            long_term_memory_mode: Long-term memory mode for ReActAgent
            max_iters: Maximum iterations for ReAct loop
            compression_config: Memory compression configuration (optional)
            resume_coordinator: Optional resume checkpoint coordinator
        """
        self.model = model
        self.formatter = formatter
        self.worker = worker
        self.memory = memory
        self.long_term_memory = long_term_memory
        self.long_term_memory_mode = long_term_memory_mode
        self.resume_coordinator = resume_coordinator
        self._resume_thread_run_id: Optional[str] = None
        self._resume_objective_fingerprint: Optional[str] = None
        self._resume_strategy: str = "auto"
        self._resume_state: Optional[ResumeState] = None
        self._parallel_tool_calls_enabled = _env_flag(
            "AGENTSCOPE_PARALLEL_TOOL_CALLS",
            True,
        )
        self._log_prompt_size_enabled = _env_flag(
            "AGENTSCOPE_LOG_PROMPT_SIZE",
            False,
        )
        
        # Create Orchestrator's toolkit
        self.toolkit = Toolkit()
        
        # Register all tools from full_toolkit if provided
        if full_toolkit is not None:
            self._register_all_tools(full_toolkit)
        
        # Register the delegation tool for deep research
        self._register_delegation_tool()
        
        # Create the ReActAgent
        self.agent = ReActAgent(
            name="Orchestrator",
            sys_prompt=get_orchestrator_prompt(),
            model=model,
            formatter=formatter,
            toolkit=self.toolkit,
            memory=memory,
            long_term_memory=long_term_memory,
            long_term_memory_mode=long_term_memory_mode,
            max_iters=max_iters,
            parallel_tool_calls=self._parallel_tool_calls_enabled,
            compression_config=compression_config,  # Memory compression
        )

        # Register delete_from_memory if the LTM backend supports it.
        # ReActAgent only auto-registers record_to_memory and retrieve_from_memory;
        # delete_from_memory is a custom extension that needs explicit registration.
        if long_term_memory and hasattr(long_term_memory, "delete_from_memory"):
            self.agent.toolkit.register_tool_function(
                long_term_memory.delete_from_memory,
            )

        self.agent._prompt_token_counter = CharTokenCounter()

        async def _log_prompt_size(agent, kwargs):
            try:
                if hasattr(agent.model, "set_cache_context"):
                    memory_obj = getattr(agent, "memory", None)
                    thread_id = str(getattr(memory_obj, "thread_id", "") or "")
                    agent.model.set_cache_context(thread_id=thread_id)
                memory_msgs = await agent.memory.get_memory()
                if hasattr(agent.model, "set_cache_context"):
                    memory_obj = getattr(agent, "memory", None)
                    thread_id = str(getattr(memory_obj, "thread_id", "") or "")
                    agent.model.set_cache_context(thread_id=thread_id)
                prompt = await agent.formatter.format(
                    msgs=[
                        Msg("system", agent.sys_prompt, "system"),
                        *memory_msgs,
                    ],
                )
                tools = agent.toolkit.get_json_schemas()
                token_estimate = await agent._prompt_token_counter.count(
                    prompt,
                    tools=tools,
                )
                logger.info(
                    "[Orchestrator] Prompt size estimate: %d chars (memory=%d, tools=%d)",
                    token_estimate,
                    len(memory_msgs),
                    len(tools),
                )
            except Exception as e:
                logger.warning(f"[Orchestrator] Prompt size logging failed: {e}")
            return None

        if self._log_prompt_size_enabled:
            self.agent.register_instance_hook(
                hook_type="pre_reasoning",
                hook_name="log_prompt_size",
                hook=_log_prompt_size,
            )

        # Micro Compact: prune old tool outputs before each LLM call
        from ..memory.micro_compact import make_micro_compact_hook
        _mc_enabled = getattr(memory, 'enable_micro_compact', True)
        self.agent.register_instance_hook(
            hook_type="pre_reasoning",
            hook_name="micro_compact",
            hook=make_micro_compact_hook(enabled=_mc_enabled),
        )

        async def _workspace_guard(agent, kwargs):
            msg = kwargs.get("msg")
            last = kwargs.get("last", True)
            if not last or not msg or getattr(msg, "role", None) != "assistant":
                return None
            updated_msg, replacements = await relocate_external_paths(
                msg,
                agent.toolkit,
                label="orchestrator",
            )
            if replacements:
                patched = dict(kwargs)
                patched["msg"] = updated_msg
                return patched
            return None

        self.agent.register_instance_hook(
            hook_type="pre_print",
            hook_name="workspace_guard",
            hook=_workspace_guard,
        )
        
        logger.info(
            "[OrchestratorAgent] Initialized with %d tools (including delegate_to_worker), parallel_tool_calls=%s",
            len(self.toolkit.get_json_schemas()),
            self._parallel_tool_calls_enabled,
        )
    
    def _register_all_tools(self, full_toolkit: Toolkit):
        """Register all tools from the full toolkit."""
        # Get all tool functions from full_toolkit
        # Note: AgentScope Toolkit stores RegisteredToolFunction objects in 'tools'
        # We need to extract the original_func to re-register
        if hasattr(full_toolkit, 'tools'):
            for tool_name, registered_tool in full_toolkit.tools.items():
                try:
                    # Extract the original function from RegisteredToolFunction
                    if hasattr(registered_tool, 'original_func'):
                        self.toolkit.register_tool_function(registered_tool.original_func)
                        logger.debug(f"[OrchestratorAgent] Registered tool: {tool_name}")
                    else:
                        logger.warning(f"[OrchestratorAgent] Tool {tool_name} has no original_func")
                except Exception as e:
                    logger.warning(f"[OrchestratorAgent] Failed to register tool {tool_name}: {e}")
    
    def _register_delegation_tool(self):
        """Register the delegate_to_worker tool for deep research tasks."""

        async def delegate_to_worker(task: str, context: str = "") -> ToolResponse:
            """Delegate a deep-research task to the Worker agent."""
            logger.info("[Orchestrator] Delegating deep research to Worker: %s", task[:100])
            max_report_chars = 80000
            min_report_chars = 9000
            min_section_headings = 4
            min_long_paragraphs = 8

            def _artifact_paths(run_id: Optional[str]) -> dict:
                run_segment = run_id or "adhoc"
                run_dir = f"research/runs/{run_segment}"
                return {
                    "run_dir": run_dir,
                    "report_run": f"{run_dir}/report.md",
                    "sources_run": f"{run_dir}/sources.json",
                    "evidence_run": f"{run_dir}/evidence.md",
                    "report_latest": "research/report.latest.md",
                    "sources_latest": "research/sources.latest.json",
                    "evidence_latest": "research/evidence.latest.md",
                }

            async def _call_tool(tool_name: str, **kwargs) -> Optional[ToolResponse]:
                tool = None
                if hasattr(self.toolkit, "tools"):
                    tool = self.toolkit.tools.get(tool_name)
                if not tool:
                    return None
                func = getattr(tool, "original_func", None) or getattr(tool, "func", None)
                if not func:
                    return None
                result = func(**kwargs)
                if hasattr(result, "__aiter__"):
                    last = None
                    async for chunk in result:
                        last = chunk
                    return last
                if hasattr(result, "__await__"):
                    result = await result
                return result

            def _tool_response_text(response: Optional[ToolResponse]) -> str:
                if response is None:
                    return ""
                content = getattr(response, "content", None)
                if isinstance(content, list):
                    texts = []
                    for block in content:
                        if isinstance(block, dict):
                            text_block = block.get("text") or block.get("content") or ""
                        else:
                            text_block = getattr(block, "text", None)
                        if text_block:
                            texts.append(str(text_block))
                    text_output = "\n".join(texts).strip()
                    if text_output.startswith("Error:"):
                        return ""
                    return text_output
                response_text = str(response).strip()
                if response_text.startswith("Error:"):
                    return ""
                return response_text

            async def _ensure_parent_dir(path_value: str) -> None:
                parent = os.path.dirname(path_value)
                if parent:
                    await _call_tool("make_dir", path=parent)

            async def _read_text(path_value: str) -> str:
                return _tool_response_text(
                    await _call_tool("read_file", path=path_value),
                ).strip()

            async def _write_text(path_value: str, value: str) -> None:
                await _ensure_parent_dir(path_value)
                await _call_tool("write_file", path=path_value, content=value)

            async def _sync_latest_artifacts(artifacts: dict) -> None:
                for run_key, latest_key in (
                    ("report_run", "report_latest"),
                    ("sources_run", "sources_latest"),
                    ("evidence_run", "evidence_latest"),
                ):
                    run_path = artifacts[run_key]
                    latest_path = artifacts[latest_key]
                    content_text = await _read_text(run_path)
                    if content_text:
                        await _write_text(latest_path, content_text)

            def _report_metrics(text: str) -> dict:
                if not text:
                    return {
                        "char_count": 0,
                        "heading_count": 0,
                        "long_paragraph_count": 0,
                        "has_table": False,
                        "has_title": False,
                        "has_date": False,
                    }
                headings = re.findall(
                    r"^(?:#{1,6}\s+.+|[一二三四五六七八九十]+、.+)$",
                    text,
                    re.M,
                )
                paragraphs = [
                    block.strip()
                    for block in re.split(r"\n\s*\n", text)
                    if block.strip()
                ]
                long_paragraphs = [paragraph for paragraph in paragraphs if len(paragraph) >= 200]
                has_table = bool(
                    re.search(r"^\|.*\|\s*$\n^\|[-:| ]+\|\s*$", text, re.M)
                )
                table_excused = bool(
                    re.search(
                        r"(table not applicable|no table|表格不适用|不适合表格)",
                        text,
                        re.IGNORECASE,
                    )
                )
                if table_excused:
                    has_table = True
                has_title = bool(text.strip().splitlines()[0].strip())
                has_date = bool(
                    re.search(
                        r"\b\d{4}[./-]\d{1,2}[./-]\d{1,2}\b|\d{4}年\d{1,2}月\d{1,2}日",
                        text,
                    )
                )
                return {
                    "char_count": len(text),
                    "heading_count": len(headings),
                    "long_paragraph_count": len(long_paragraphs),
                    "has_table": has_table,
                    "has_title": has_title,
                    "has_date": has_date,
                }

            def _missing_report_requirements(metrics: dict) -> list[str]:
                missing_items = []
                if metrics["char_count"] < min_report_chars:
                    missing_items.append(f"length >= {min_report_chars} chars")
                if metrics["heading_count"] < min_section_headings:
                    missing_items.append(f"{min_section_headings}+ section headings")
                if metrics["long_paragraph_count"] < min_long_paragraphs:
                    missing_items.append(
                        f"{min_long_paragraphs}+ substantial paragraphs"
                    )
                if not metrics["has_table"]:
                    missing_items.append("comparison table or note why not applicable")
                if not metrics["has_title"]:
                    missing_items.append("title line")
                if not metrics["has_date"]:
                    missing_items.append("date line")
                return missing_items

            def _build_quality_gate_summary(text: str, report_text: str = "") -> str:
                urls = re.findall(r"https?://[^\s)]+", text)
                metrics = _report_metrics(report_text or text)
                has_sources_section = any(
                    marker in text for marker in ("来源清单", "Sources Consulted", "Sources")
                )
                has_evidence_table = any(
                    marker in text
                    for marker in (
                        "证据表",
                        "Evidence Table",
                        "Claim → Evidence",
                        "Claim -> Evidence",
                    )
                )
                has_uncertainty = any(
                    marker in text for marker in ("不确定", "矛盾", "uncertainty", "conflict")
                )
                has_report_file = "/workspace/research/report.latest.md" in text
                has_sources_file = "/workspace/research/sources.latest.json" in text
                has_evidence_file = "/workspace/research/evidence.latest.md" in text

                missing = []
                if not urls:
                    missing.append("urls")
                if not has_sources_section:
                    missing.append("sources_section")
                if not has_evidence_table:
                    missing.append("evidence_table")
                if not has_report_file:
                    missing.append("report_file")
                if not has_sources_file:
                    missing.append("sources_file")
                if not has_evidence_file:
                    missing.append("evidence_file")
                if metrics["char_count"] < min_report_chars:
                    missing.append("report_length")
                if metrics["heading_count"] < min_section_headings:
                    missing.append("section_headings")
                if metrics["long_paragraph_count"] < min_long_paragraphs:
                    missing.append("paragraph_depth")
                if not metrics["has_table"]:
                    missing.append("comparison_table")
                if not metrics["has_title"]:
                    missing.append("title_line")
                if not metrics["has_date"]:
                    missing.append("date_line")

                status = "PASS" if not missing else "FAIL"
                summary_lines = [
                    "## Quality Gate (Auto)",
                    f"- status: {status}",
                    f"- urls_found: {len(set(urls))}",
                    f"- sources_section: {'yes' if has_sources_section else 'no'}",
                    f"- evidence_table: {'yes' if has_evidence_table else 'no'}",
                    f"- uncertainty_section: {'yes' if has_uncertainty else 'no'}",
                    f"- report_file: {'yes' if has_report_file else 'no'}",
                    f"- sources_file: {'yes' if has_sources_file else 'no'}",
                    f"- evidence_file: {'yes' if has_evidence_file else 'no'}",
                    f"- report_chars: {metrics['char_count']}",
                    f"- section_headings: {metrics['heading_count']}",
                    f"- long_paragraphs: {metrics['long_paragraph_count']}",
                    f"- has_table: {'yes' if metrics['has_table'] else 'no'}",
                    f"- has_title: {'yes' if metrics['has_title'] else 'no'}",
                    f"- has_date: {'yes' if metrics['has_date'] else 'no'}",
                ]
                if missing:
                    summary_lines.append(f"- missing: {', '.join(missing)}")
                return "\n".join(summary_lines)

            async def _save_checkpoint(
                *,
                phase: str,
                completed_steps: list[str],
                next_step: str,
                artifacts: dict,
                summary_text: str = "",
            ) -> None:
                if not self.resume_coordinator:
                    return

                objective_fingerprint = self._resume_objective_fingerprint
                if not objective_fingerprint:
                    objective_fingerprint = self.resume_coordinator.build_objective_fingerprint(
                        f"{task}\n{context}",
                    )

                run_id_for_state = (
                    self._resume_thread_run_id
                    or get_agent_run_context()[0]
                    or "unknown"
                )

                try:
                    state = ResumeState(
                        thread_id=(
                            getattr(self.memory, "thread_id", None)
                            or getattr(self._resume_state, "thread_id", "")
                        ),
                        project_id=(
                            getattr(self.memory, "project_id", None)
                            or getattr(self._resume_state, "project_id", None)
                        ),
                        objective_fingerprint=objective_fingerprint,
                        last_run_id=run_id_for_state,
                        mode="deep_research",
                        phase=phase,
                        completed_steps=completed_steps,
                        next_step=next_step,
                        artifacts={
                            "report_latest": artifacts["report_latest"],
                            "sources_latest": artifacts["sources_latest"],
                            "evidence_latest": artifacts["evidence_latest"],
                            "run_snapshot_dir": artifacts["run_dir"],
                        },
                        summary=summary_text,
                        updated_at=datetime.now(timezone.utc).isoformat(),
                    )
                    await self.resume_coordinator.save_state(
                        state,
                        thread_run_id=run_id_for_state,
                    )
                except Exception as exc:
                    logger.warning("[Orchestrator] Failed to persist resume checkpoint: %s", exc)

            # Build the message for Worker
            agent_run_id, _ = get_agent_run_context()
            if not agent_run_id:
                agent_run_id = self._resume_thread_run_id
            artifacts = _artifact_paths(agent_run_id)

            resume_snapshot = ""
            if self._resume_state and self._resume_strategy != "fresh":
                resume_snapshot = (
                    "\n\n## Resume Snapshot\n"
                    f"- Last phase: {self._resume_state.phase}\n"
                    f"- Next step: {self._resume_state.next_step or '(not specified)'}\n"
                    f"- Reuse /workspace/{self._resume_state.artifacts.get('report_latest', artifacts['report_latest'])}"
                )

            worker_message = f"## Deep Research Task\n{task}"
            if context:
                worker_message += f"\n\n{context}"
            worker_message += resume_snapshot
            worker_message += (
                "\n\n## File Naming (MANDATORY)\n"
                f"- Run snapshot report: /workspace/{artifacts['report_run']}\n"
                f"- Run snapshot sources: /workspace/{artifacts['sources_run']}\n"
                f"- Run snapshot evidence: /workspace/{artifacts['evidence_run']}\n"
                f"- Stable latest report: /workspace/{artifacts['report_latest']}\n"
                f"- Stable latest sources: /workspace/{artifacts['sources_latest']}\n"
                f"- Stable latest evidence: /workspace/{artifacts['evidence_latest']}\n"
                "- You MAY use temp paths, but all critical artifacts must be synced to the run snapshot paths above."
            )

            await _ensure_parent_dir(artifacts["report_run"])
            await _save_checkpoint(
                phase="planning",
                completed_steps=["Delegation payload prepared"],
                next_step="Run worker investigation and collect evidence",
                artifacts=artifacts,
                summary_text=(context or task)[:1000],
            )

            worker_msg = Msg(
                name="Orchestrator",
                content=worker_message,
                role="user",
            )

            try:
                # Execute via Worker
                result = await self.worker(worker_msg)
                worker_report_text = (
                    result.get_text_content()
                    if hasattr(result, "get_text_content")
                    else str(result)
                )

                report_text = await _read_text(artifacts["report_run"])
                if not report_text:
                    report_text = await _read_text(artifacts["report_latest"])

                if not report_text:
                    create_message = (
                        "Create or update the full research report in "
                        f"/workspace/{artifacts['report_run']}. Include title/date, "
                        "executive summary, methodology, numbered sections with "
                        "paragraphs, and a comparison table (or note why not applicable). "
                        "Preserve citations. Do not paste the full report in your response."
                    )
                    create_msg = Msg(
                        name="Orchestrator",
                        content=create_message,
                        role="user",
                    )
                    await self.worker(create_msg)
                    report_text = await _read_text(artifacts["report_run"])

                if not report_text and worker_report_text:
                    await _write_text(artifacts["report_run"], worker_report_text)
                    report_text = worker_report_text

                if report_text:
                    missing_requirements = _missing_report_requirements(
                        _report_metrics(report_text)
                    )
                    if missing_requirements:
                        expand_message = (
                            "Expand and revise the report in "
                            f"/workspace/{artifacts['report_run']} to satisfy: "
                            f"{', '.join(missing_requirements)}. "
                            "Preserve citations and structure; avoid bullet-only sections."
                        )
                        expand_msg = Msg(
                            name="Orchestrator",
                            content=expand_message,
                            role="user",
                        )
                        expand_result = await self.worker(expand_msg)
                        worker_report_text = (
                            expand_result.get_text_content()
                            if hasattr(expand_result, "get_text_content")
                            else str(expand_result)
                        )
                        refreshed_report = await _read_text(artifacts["report_run"])
                        if refreshed_report:
                            report_text = refreshed_report

                sources_text = await _read_text(artifacts["sources_run"])
                if not sources_text:
                    sources_text = await _read_text(artifacts["sources_latest"])
                evidence_text = await _read_text(artifacts["evidence_run"])
                if not evidence_text:
                    evidence_text = await _read_text(artifacts["evidence_latest"])

                await _sync_latest_artifacts(artifacts)

                report_latest_text = await _read_text(artifacts["report_latest"])
                sources_latest_text = await _read_text(artifacts["sources_latest"])
                evidence_latest_text = await _read_text(artifacts["evidence_latest"])

                checkpoint_summary = (
                    (report_latest_text[:1200] or report_text[:1200])
                    + ("\n\nSources excerpt:\n" + sources_latest_text[:600] if sources_latest_text else "")
                    + ("\n\nEvidence excerpt:\n" + evidence_latest_text[:600] if evidence_latest_text else "")
                )
                await _save_checkpoint(
                    phase="writing",
                    completed_steps=[
                        "Worker investigation executed",
                        "Run snapshot artifacts generated",
                        "Stable latest artifacts synced",
                    ],
                    next_step="Run quality gate and finalize report response",
                    artifacts=artifacts,
                    summary_text=checkpoint_summary,
                )

                result_text = worker_report_text
                if report_latest_text:
                    result_text += (
                        f"\n\n## Report File Content (/workspace/{artifacts['report_latest']})\n"
                        f"{report_latest_text}"
                    )
                if evidence_latest_text:
                    result_text += (
                        f"\n\n## Evidence File Content (/workspace/{artifacts['evidence_latest']})\n"
                        f"{evidence_latest_text}"
                    )
                if sources_latest_text:
                    result_text += (
                        f"\n\n## Sources File Content (/workspace/{artifacts['sources_latest']})\n"
                        f"{sources_latest_text}"
                    )

                result_text += (
                    "\n\n## Stable Artifacts\n"
                    f"- /workspace/{artifacts['report_latest']}\n"
                    f"- /workspace/{artifacts['sources_latest']}\n"
                    f"- /workspace/{artifacts['evidence_latest']}"
                )

                quality_gate = _build_quality_gate_summary(
                    result_text,
                    report_latest_text or report_text,
                )
                if len(result_text) > max_report_chars:
                    truncated = result_text[: max(0, max_report_chars - len(quality_gate) - 64)]
                    result_text = (
                        truncated
                        + "\n\n[Truncated] Full worker report was streamed in the conversation."
                    )
                result_text = f"{result_text}\n\n{quality_gate}"

                await _save_checkpoint(
                    phase="finalized",
                    completed_steps=[
                        "Worker investigation executed",
                        "Quality gate evaluated",
                        "Response finalized",
                    ],
                    next_step="Wait for follow-up or continuation request",
                    artifacts=artifacts,
                    summary_text=(report_latest_text or report_text or worker_report_text)[:1600],
                )

                logger.info(
                    "[Orchestrator] Worker completed research, result length: %d",
                    len(result_text),
                )

                return ToolResponse(
                    content=[TextBlock(type="text", text=f"## Worker Research Report\n{result_text}")],
                )
            except Exception as exc:
                logger.error("[Orchestrator] Worker execution failed: %s", exc)
                await _save_checkpoint(
                    phase="failed",
                    completed_steps=["Worker execution failed"],
                    next_step="Re-run deep research from latest stable artifacts",
                    artifacts=artifacts,
                    summary_text=str(exc),
                )
                return ToolResponse(
                    content=[TextBlock(type="text", text=f"## Worker Error\n{str(exc)}")],
                )

        self.toolkit.register_tool_function(delegate_to_worker)

    def set_resume_context(
        self,
        thread_run_id: Optional[str],
        objective_fingerprint: Optional[str],
        resume_strategy: str = "auto",
        resume_state: Optional[ResumeState] = None,
    ) -> None:
        """Attach per-run resume metadata used by delegate_to_worker."""
        self._resume_thread_run_id = thread_run_id
        self._resume_objective_fingerprint = objective_fingerprint
        self._resume_strategy = resume_strategy
        self._resume_state = resume_state

    async def __call__(self, msg):
        """
        Process a user request.
        
        Args:
            msg: AgentScope Msg with user request
            
        Returns:
            AgentScope Msg with final response
        """
        return await self.agent(msg)
    
    def set_console_output_enabled(self, enabled: bool):
        """Enable or disable console output."""
        self.agent.set_console_output_enabled(enabled)
    
    def get_agent(self) -> ReActAgent:
        """Get the underlying ReActAgent."""
        return self.agent
