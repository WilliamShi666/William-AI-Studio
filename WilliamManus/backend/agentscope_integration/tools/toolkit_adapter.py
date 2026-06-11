"""
Toolkit Adapter for AgentScope Integration

Adapts all existing tools to AgentScope's Toolkit format.
This ensures that all sandbox tools, skills, and browser tools
work seamlessly with AgentScope agents.

Adapted tools:
- SandboxCodeTool: execute_command, read_file, write_file (streaming), edit_file, list_dir, make_dir, upload_file, download_file, expose_port
- SandboxWebSearchTool: web_search, scrape_webpage
- SandboxSkillTool: get_available_skills, load_skill, load_reference, list_skill_scripts, run_skill_script
- ComputerUseTool: computer_use
- SandboxBrowserTool: browser_navigate_to, browser_act, browser_extract_content, browser_close
- TaskListTool: create_tasks, view_tasks, update_tasks, delete_tasks
"""

import json
import asyncio
import hashlib
import inspect
import os
import re
import shlex
from typing import AsyncGenerator, Optional, Dict, Any, Callable, List, Set
from functools import wraps

from agentscope.tool import Toolkit, ToolResponse
from agentscope.message import TextBlock
from agentpress.tool import ToolResult

from agentscope_integration.shadow_clone.recovery_escalation import (
    ShadowCloneSandboxFatalToolError,
    maybe_raise_shadow_clone_fatal_tool_error,
)
from eval.safety_checker import (
    classify_command_safety,
    classify_strict_attach_refusal,
    classify_tool_violation,
)
from services.eval_runtime_registry import record_safety_event, record_tool_event
from agentscope_integration.utils.tool_arg_merge import sanitize_write_file_input
from utils.agent_run_context import get_agent_run_context
from utils.config import config
from utils.logger import logger


# ── File Read Cache (lightweight readFileState) ────────────────────────────

class FileReadEntry:
    """Snapshot of a file at the time it was read by the agent."""
    __slots__ = ("content", "content_hash", "timestamp")

    def __init__(self, content: str):
        self.content = content
        self.content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.timestamp = __import__("time").time()


class FileReadCache:
    """In-memory LRU cache tracking which files an agent has read.

    Provides the foundation for pre-read enforcement and modification
    detection on edit_file / write_file calls.
    """

    def __init__(self, max_entries: int = 100):
        self._max_entries = max(1, max_entries)
        self._cache: dict[str, FileReadEntry] = {}

    def get(self, path: str) -> "Optional[FileReadEntry]":
        key = str(path or "").strip()
        return self._cache.get(key) if key else None

    def set(self, path: str, entry: FileReadEntry) -> None:
        key = str(path or "").strip()
        if not key:
            return
        if key in self._cache:
            del self._cache[key]
        elif len(self._cache) >= self._max_entries:
            oldest = next(iter(self._cache))
            del self._cache[oldest]
        self._cache[key] = entry

    def invalidate(self, path: str) -> None:
        key = str(path or "").strip()
        self._cache.pop(key, None)


# ── Shared required-params registry ────────────────────────────────────────
# Populated by _register_tool / _register_streaming_tool at tool registration
# time.  Read by openrouter_model.py to decide when a tool call should be
# converted to a TextBlock instead of being dispatched with empty arguments.
# Keys are tool names (str), values are lists of required parameter names.
_TOOL_REQUIRED_PARAMS_REGISTRY: dict[str, list[str]] = {}

# Fallback for tools not yet in the registry (mirrors openrouter_model.py).
# Prevents the pre-execution gate from silently skipping empty-param checks
# during early startup before _register_tool() has run for every tool.
_TOOL_REQUIRED_PARAMS_FALLBACK: dict[str, list[str]] = {
    "execute_command": ["command"],
    "write_file": ["path", "content"],
    "read_file": ["path"],
    "edit_file": ["path", "old_text", "new_text"],
    "create_tasks": ["section_title", "task_contents"],
}


def get_tool_required_params(tool_name: str) -> list[str]:
    """Return required parameter names for *tool_name* from the registry.

    Returns required params, checking registry first then fallback.
    Returns an empty list when neither source has the tool.
    """
    key = str(tool_name or "")
    params = _TOOL_REQUIRED_PARAMS_REGISTRY.get(key)
    if params is not None:
        return params
    return _TOOL_REQUIRED_PARAMS_FALLBACK.get(key, [])


def _extract_required_params(func) -> list[str]:
    """Extract required parameter names from a function signature.

    Parameters without default values are considered required.
    Skips ``self`` / ``cls``, ``*args``, and ``**kwargs``.
    Returns an empty list when the signature cannot be introspected.
    """
    try:
        sig = inspect.signature(func)
        return [
            name
            for name, param in sig.parameters.items()
            if param.default is inspect.Parameter.empty
            and name not in ("self", "cls")
            and param.kind
            not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
    except (TypeError, ValueError):
        return []


def _check_missing_params(
    tool_name: str,
    kwargs: dict,
    required_params: list[str],
) -> list[str]:
    """Return names of required params that are missing or have empty values.

    A parameter is considered missing when its value is ``None``, an empty
    string (or whitespace-only), an empty bytes/bytearray, or an empty
    list/dict.  Falsy-but-meaningful values like ``0`` and ``False`` are
    treated as valid.
    """
    missing = []
    for param in required_params:
        value = kwargs.get(param)
        if value is None:
            missing.append(param)
        elif isinstance(value, str) and not value.strip():
            missing.append(param)
        elif isinstance(value, (bytes, bytearray)) and not value:
            missing.append(param)
        elif isinstance(value, (list, dict)) and len(value) == 0:
            missing.append(param)
    return missing


def _inject_deepseek_strict_into_schema(json_schema: dict) -> None:
    """Inject DeepSeek strict-mode fields into a tool's JSON schema.

    Adds ``strict: true`` to the function object and
    ``additionalProperties: false`` to the parameters object.  Also
    removes schema keywords that DeepSeek's strict mode does not
    support (minLength, maxLength, minItems, maxItems).

    Only called when ``AGENTSCOPE_DEEPSEEK_STRICT_MODE`` is enabled.
    """
    func = json_schema.get("function")
    if not isinstance(func, dict):
        return

    func["strict"] = True

    params = func.get("parameters")
    if isinstance(params, dict) and params.get("type") == "object":
        params["additionalProperties"] = False
        _clean_unsupported_strict_keywords(params)


def _clean_unsupported_strict_keywords(schema: dict) -> None:
    """Recursively remove JSON Schema keywords unsupported by DeepSeek strict mode.

    Unsupported: minLength, maxLength, minItems, maxItems.
    """
    unsupported = {"minLength", "maxLength", "minItems", "maxItems"}
    for key in unsupported:
        schema.pop(key, None)

    for value in schema.values():
        if isinstance(value, dict):
            _clean_unsupported_strict_keywords(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _clean_unsupported_strict_keywords(item)


# ── Character-loss detection patterns ────────────────────────────────

_CHAR_LOSS_PATTERNS = [
    (r"\bfor\s+in\s+\[", "for VAR in [ ... ] — missing loop variable after 'for'"),
    (r"\bfor\s+in\s+range\b", "for VAR in range(...) — missing loop variable after 'for'"),
    (r"\\frac\s+\d+\s+[a-zA-Z]", r"\frac{NUM}{DEN} — missing curly braces around fraction arguments"),
    (r"\\frac\s+\d+[^}]*?$", r"\frac{NUM}{DEN} — likely missing braces around fraction arguments"),
]


def _detect_character_loss(error_text: str) -> "str | None":
    """Return a hint if *error_text* matches known character-loss patterns."""
    if not error_text:
        return None
    for pattern, description in _CHAR_LOSS_PATTERNS:
        if re.search(pattern, error_text):
            return (
                "CHARACTER LOSS DETECTED: The command output matches "
                f"'{description}'. Single characters (loop variables, "
                "braces) may have been dropped during generation. "
                "Fix: use sed -i to insert the missing character, or "
                "re-read the file, copy the broken line as old_text, "
                "and set the corrected version as new_text."
            )
    return None


def _extract_workspace_paths_from_command(command: str) -> list[str]:
    matches = re.findall(r"['\"](/workspace/[^'\"\s;|&<>]+)['\"]", str(command or ""))
    matches.extend(re.findall(r"(?<![\w/])(/workspace/[^\s;|&<>]+)", str(command or "")))
    paths: list[str] = []
    for match in matches:
        path = str(match or "").strip().rstrip(".,)\"'")
        if path and path not in paths:
            paths.append(path)
    return paths


def _is_read_only_file_inspection_command(command: str) -> bool:
    cmd = str(command or "").strip()
    if not cmd:
        return False
    if re.search(r"(?:^|[;&|]\s*)(?:grep|egrep|fgrep|sed\s+-n|cat|nl\s+-ba)\b", cmd):
        return bool(_extract_workspace_paths_from_command(cmd))
    return False


class ToolkitAdapter:
    """
    Adapts all existing tools to AgentScope Toolkit format.
    
    This class wraps existing tool instances and registers their methods
    as AgentScope-compatible tool functions.
    """
    
    def __init__(
        self,
        project_id: str,
        thread_id: str,
        thread_manager=None,
        db_client=None,
        *,
        shadow_clone_run_id: Optional[str] = None,
        strict_sandbox: bool = False,
        shadow_clone_fail_fast: bool = False,
        trace=None,
    ):
        """
        Initialize the toolkit adapter.
        
        Args:
            project_id: Project ID for sandbox tools
            thread_id: Thread ID for this conversation
            thread_manager: Thread manager instance
            db_client: Database client for task list tool
        """
        self.project_id = project_id
        self.thread_id = thread_id
        self.thread_manager = thread_manager
        self.db_client = db_client
        self.shadow_clone_run_id = str(shadow_clone_run_id or "").strip() or None
        self.strict_sandbox = bool(strict_sandbox and self.shadow_clone_run_id)
        self.shadow_clone_fail_fast = bool(
            shadow_clone_fail_fast and self.strict_sandbox
        )
        self._strict_shadow_clone_sandbox_type = (
            config.get_shadow_clone_sandbox_type() if self.strict_sandbox else None
        )

        self._tool_output_max_chars = int(
            os.getenv("AGENTSCOPE_TOOL_OUTPUT_MAX_CHARS", "12000")
        )
        self._browser_tool_mode = os.getenv(
            "AGENTSCOPE_BROWSER_TOOL_MODE",
            "desktop_compat",
        ).strip().lower()
        self._use_legacy_browser_tools = self._browser_tool_mode in {
            "legacy",
            "browser",
            "browser_sandbox",
        }
        if not self._use_legacy_browser_tools and self._browser_tool_mode != "desktop_compat":
            logger.warning(
                "[ToolkitAdapter] Unknown AGENTSCOPE_BROWSER_TOOL_MODE=%s, fallback to desktop_compat",
                self._browser_tool_mode,
            )
            self._browser_tool_mode = "desktop_compat"

        self._has_image_media_refs = False
        self._enforce_ocr_guard = False
        self._multimodal_uploaded_image_paths: Set[str] = set()
        self._ocr_command_patterns = (
            r"\btesseract\b",
            r"\bpytesseract\b",
            r"\beasyocr\b",
            r"\bocrmypdf\b",
            r"\bpaddleocr\b",
        )
        
        self.toolkit = Toolkit()
        self._tool_instances: Dict[str, Any] = {}
        self._tool_factories: Dict[str, Callable[[], Any]] = {}
        self._registered_tool_functions: List[Callable] = []
        self._lf_trace = trace
        self.file_read_cache = FileReadCache()

        # Initialize and register all tools
        self._init_tool_instances()
        self._register_all_tools()
    
    def _init_tool_instances(self):
        """Initialize eager tool instances and prepare lazy backends."""
        try:
            from agent.tools.task_list_tool import TaskListTool

            self._tool_factories["code"] = self._create_code_tool
            self._tool_factories["web_search"] = self._create_web_search_tool
            self._tool_factories["skill"] = self._create_skill_tool
            self._tool_factories["media"] = self._create_media_tool

            if not self.strict_sandbox or self._strict_shadow_clone_sandbox_type == "desktop":
                self._tool_factories["computer_use"] = self._create_computer_use_tool
            else:
                logger.info(
                    "[ToolkitAdapter] Skipping ComputerUseTool for strict Shadow Clone run %s "
                    "because shared sandbox type is %s",
                    self.shadow_clone_run_id,
                    self._strict_shadow_clone_sandbox_type,
                )

            if self._use_legacy_browser_tools:
                self._tool_factories["browser"] = self._create_legacy_browser_tool

            self._tool_instances["task_list"] = TaskListTool(
                project_id=self.project_id,
                thread_manager=self.thread_manager,
                thread_id=self.thread_id,
            )

            logger.info(
                "[ToolkitAdapter] Initialized %d eager tool instances and prepared %d lazy backends",
                len(self._tool_instances),
                len(self._tool_factories),
            )

        except Exception as e:
            logger.error(f"[ToolkitAdapter] Failed to initialize tool instances: {e}")
            raise

    def _create_code_tool(self):
        from agent.tools.sandbox_code_tool import SandboxCodeTool

        return SandboxCodeTool(
            project_id=self.project_id,
            thread_manager=self.thread_manager,
            shadow_clone_run_id=self.shadow_clone_run_id,
            strict_sandbox=self.strict_sandbox,
        )

    def _create_media_tool(self):
        from agent.tools.kimi_media_understanding_tool import KimiMediaUnderstandingTool

        return KimiMediaUnderstandingTool(code_tool=self._ensure_tool_instance("code"))

    def _create_web_search_tool(self):
        from agent.tools.sandbox_web_search_tool import SandboxWebSearchTool

        return SandboxWebSearchTool(
            project_id=self.project_id,
            thread_manager=self.thread_manager,
            shadow_clone_run_id=self.shadow_clone_run_id,
            strict_sandbox=self.strict_sandbox,
        )

    def _create_skill_tool(self):
        from agent.tools.sandbox_skill_tool import SandboxSkillTool

        return SandboxSkillTool(
            project_id=self.project_id,
            thread_manager=self.thread_manager,
            shadow_clone_run_id=self.shadow_clone_run_id,
            strict_sandbox=self.strict_sandbox,
        )

    def _create_computer_use_tool(self):
        from agent.tools.computer_use_tool import ComputerUseTool

        return ComputerUseTool(
            project_id=self.project_id,
            thread_manager=self.thread_manager,
            shadow_clone_run_id=self.shadow_clone_run_id,
            strict_sandbox=self.strict_sandbox,
        )

    def _create_legacy_browser_tool(self):
        from agent.tools.sb_browser_tool import SandboxBrowserTool

        return SandboxBrowserTool(
            project_id=self.project_id,
            thread_manager=self.thread_manager,
            shadow_clone_run_id=self.shadow_clone_run_id,
            strict_sandbox=self.strict_sandbox,
        )

    def _has_tool_kind(self, kind: str) -> bool:
        normalized = str(kind or "").strip()
        return normalized in self._tool_instances or normalized in self._tool_factories

    def _ensure_tool_instance(self, kind: str):
        normalized = str(kind or "").strip()
        if not normalized:
            return None

        tool = self._tool_instances.get(normalized)
        if tool is not None:
            return tool

        factory = self._tool_factories.get(normalized)
        if factory is None:
            return None

        try:
            tool = factory()
        except Exception as exc:
            logger.error(
                "[ToolkitAdapter] Failed to lazy-initialize tool backend %s: %s",
                normalized,
                exc,
            )
            raise

        self._tool_instances[normalized] = tool
        logger.info("[ToolkitAdapter] Lazy-initialized tool backend: %s", normalized)
        return tool

    async def _call_tool_method(
        self,
        kind: str,
        method_name: str,
        *,
        unavailable_error: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        _lf_span = None
        active_run_id = self.shadow_clone_run_id or get_agent_run_context()[0]
        if self._lf_trace:
            try:
                safe_input = {
                    k: (str(v)[:500] if isinstance(v, str) and len(str(v)) > 500 else v)
                    for k, v in kwargs.items()
                }
                _lf_span = self._lf_trace.start_observation(
                    name=f"tool:{kind}.{method_name}",
                    as_type="span",
                    input={"kind": kind, "method": method_name, **safe_input},
                )
            except Exception:
                pass

        result = None
        try:
            tool = self._ensure_tool_instance(kind)
            if tool is None:
                error = unavailable_error or f"Tool backend {kind} is not available"
                result = ToolResult(success=False, output={"error": error})
                return result

            method = getattr(tool, method_name, None)
            if not callable(method):
                raise RuntimeError(
                    f"Tool backend {kind} does not provide method {method_name}",
                )
            result = await method(**kwargs)
            return result
        finally:
            if _lf_span:
                try:
                    output_val = None
                    level = "DEFAULT"
                    if result is not None:
                        output_str = str(result.output or "")
                        output_val = {
                            "success": result.success,
                            "output": output_str[:1000],
                        }
                        if not result.success:
                            level = "WARNING"
                    _lf_span.update(output=output_val, level=level)
                    _lf_span.end()
                except Exception:
                    pass
    
    def _register_all_tools(self):
        """Register all tools to the AgentScope Toolkit."""
        
        # === SandboxCodeTool ===
        if self._has_tool_kind("code"):
            self._register_tool(self._execute_command_guarded, "execute_command", 
                """Run a shell command inside the code sandbox.
                
                Args:
                    command (str): The shell command to execute
                    workdir (str): Working directory (relative to /workspace)
                    timeout (int): Command timeout in seconds (default: 300)
                """)
            
            self._register_tool(self._read_file_guarded, "read_file",
                """Read a file from /workspace.
                
                Args:
                    path (str): File path relative to /workspace
                    max_bytes (int): Optional max bytes to read (capped)
                    offset (int): Optional byte offset to start reading
                    line_start (int): Optional 1-based starting line for targeted reads
                    line_end (int): Optional 1-based ending line for targeted reads
                """)
            
            # write_file uses streaming
            self._register_streaming_tool(self._write_file_guarded, "write_file",
                """Write text content to a file under /workspace.
                
                Args:
                    path (str): File path relative to /workspace
                    content (str): Content to write
                """)

            self._register_tool(self._edit_file_guarded, "edit_file",
                """Edit an existing file by replacing exact text — the PRIMARY tool
                for all file modifications after the initial write_file.

                IMPORTANT: You MUST read_file first — the tool rejects edits on
                files that haven't been read. The line_numbered_content field in
                read_file output shows exact indentation to copy.

                CRITICAL: old_text must match file content EXACTLY — every
                space, tab, and line break counts. Copy it verbatim from read_file
                output (after the → arrow in line_numbered_content). Do NOT
                re-type or reformat.
                old_text and new_text MUST be different — calling edit_file
                with identical old/new text is a no-op and will not modify
                anything.

                ALWAYS prefer editing existing files in the codebase.
                NEVER write new files unless explicitly required.
                NEVER write fix_xxx.py helper scripts to patch other files.

                To replace multiple lines, include enough surrounding context
                in old_text to uniquely identify the region you want to change.
                There is no separate line-range mode — anchor-replace covers
                every use case.

                The edit will FAIL if old_text is not unique in the file.
                Either provide a larger string with more surrounding context
                to make it unique, or set replace_all to True to change every
                occurrence.

                Args:
                    path (str): File path relative to /workspace
                    old_text (str): The text to replace — MUST copy verbatim from read_file
                    new_text (str): The text to replace it with — MUST be different from old_text
                    replace_all (bool): Set to True to replace ALL occurrences (default False).
                        When False, old_text must match exactly ONE location.
                """)
            
            self._register_tool(self._list_dir_guarded, "list_dir",
                """List files in a directory.
                
                Args:
                    path (str): Directory path relative to /workspace (default: ".")
                """)
            
            self._register_tool(self._make_dir_guarded, "make_dir",
                """Create a directory (equivalent to mkdir -p).
                
                Args:
                    path (str): Directory path to create
                """)
            
            self._register_tool(self._upload_file_guarded, "upload_file",
                """Upload a base64-encoded file to /workspace.
                
                Args:
                    path (str): Destination path
                    content_base64 (str): Base64-encoded file content
                """)
            
            self._register_tool(self._download_file_guarded, "download_file",
                """Download a file from the sandbox.
                
                Args:
                    path (str): File path to download
                    as_base64 (bool): Return as base64 string (default: False)
                """)
            
            self._register_tool(self._expose_port_guarded, "expose_port",
                """Expose a port from the sandbox for external access.
                
                Args:
                    port (int): Port number to expose
                """)

        # === KimiMediaUnderstandingTool ===
        if self._has_tool_kind("media"):
            self._register_tool(self._understand_media_guarded, "understand_media",
                """Understand an image or video file from /workspace using the Moonshot/Kimi external API.

                This sends the selected sandbox file bytes to Moonshot/Kimi and returns only a concise summary,
                metadata, and safety-limited observations. Use this instead of dumping binary bytes into context.

                Args:
                    path (str): Sandbox /workspace path, e.g. /workspace/demo.png or demo.mp4
                    media_type (str): One of auto, image, video (default: auto)
                    prompt (str): Optional analysis instructions
                    max_output_chars (int): Summary cap from 200 to 12000 (default: 4000)
                    detail_level (str): One of brief, normal, detailed (default: normal)
                    timeout_seconds (int): Kimi API timeout from 5 to 120 seconds (default: 60)
                """)
        
        # === SandboxWebSearchTool ===
        if self._has_tool_kind("web_search"):
            self._register_tool(self._web_search_tool, "web_search",
                """Search the web using Tavily API.

                Args:
                    query (str): Search query
                    max_results (int): Maximum number of results (default: 5)
                """)

            self._register_tool(self._scrape_webpage_tool, "scrape_webpage",
                """Extract full text from one or more web pages (Tavily Extract with Firecrawl fallback).

                Args:
                    urls (str): Comma-separated URLs to scrape
                    query (str): Optional rerank query for Tavily Extract
                    chunks_per_source (int): Max chunks per source when query is set (1-5)
                    extract_depth (str): "basic" or "advanced"
                    format (str): "markdown" or "text"
                    include_images (bool): Include images in Tavily Extract response
                    include_favicon (bool): Include favicon in Tavily Extract response
                    timeout (float): Timeout in seconds for Tavily Extract (1-60)
                """)

            self._register_tool(self._download_images_tool, "download_images",
                """Batch download images from URLs to the sandbox.

                Use this tool to download images from web_search results.
                Images are saved to /workspace/images/{folder_name}/.

                Args:
                    urls (str): Comma-separated image URLs to download
                    folder_name (str): Optional subfolder name (default: timestamp)
                """)
        
        # === SandboxSkillTool ===
        if self._has_tool_kind("skill"):
            self._register_tool(self._get_available_skills_tool, "get_available_skills",
                """List available document processing skills.
                
                Returns list of available skills with descriptions.
                """)
            
            self._register_tool(self._load_skill_tool, "load_skill",
                """Load skill instructions for a specific skill.
                
                Args:
                    skill_name (str): Name of the skill to load
                """)
            
            self._register_tool(self._load_reference_tool, "load_reference",
                """Load reference documentation for a skill.
                
                Args:
                    skill_name (str): Name of the skill
                    reference_name (str): Name of the reference document
                """)
            
            self._register_tool(self._list_skill_scripts_tool, "list_skill_scripts",
                """List available scripts for a skill.
                
                Args:
                    skill_name (str): Name of the skill
                """)
            
            self._register_tool(self._run_skill_script_tool, "run_skill_script",
                """Execute a skill script.
                
                Args:
                    skill_name (str): Name of the skill
                    script_name (str): Name of the script to run
                    args (dict): Arguments to pass to the script
                """)
        
        # === ComputerUseTool ===
        if self._has_tool_kind("computer_use"):
            self._register_tool(self._screenshot_tool, "screenshot",
                """Take a screenshot of the desktop.
                
                Returns a base64-encoded image of the current screen.
                """)
            
            self._register_tool(self._click_tool, "click",
                """Click at a specific position on the screen.
                
                Args:
                    x (int): X coordinate
                    y (int): Y coordinate
                    button (str): Mouse button (left, right, middle)
                    num_clicks (int): Number of clicks (default: 1)
                """)
            
            self._register_tool(self._typing_tool, "typing",
                """Type text on the keyboard.
                
                Args:
                    text (str): Text to type
                """)
            
            self._register_tool(self._scroll_tool, "scroll",
                """Scroll the mouse wheel.
                
                Args:
                    amount (int): Scroll amount (positive = up, negative = down)
                """)
            
            self._register_tool(self._press_tool, "press",
                """Press keyboard keys.
                
                Args:
                    keys (str): Key combination (e.g., "ctrl+c", "enter")
                """)
            
            self._register_tool(self._move_to_tool, "move_to",
                """Move the mouse cursor to a position.
                
                Args:
                    x (float): X coordinate
                    y (float): Y coordinate
                """)
            
            self._register_tool(self._drag_to_tool, "drag_to",
                """Drag the mouse to a position.
                
                Args:
                    x (float): X coordinate
                    y (float): Y coordinate
                """)
        
        # === Browser tools ===
        if self._use_legacy_browser_tools:
            if self._has_tool_kind("browser"):
                self._register_tool(self._legacy_browser_navigate_to, "browser_navigate_to",
                    """Navigate browser to a URL (legacy browser sandbox mode).
                    
                    Args:
                        url (str): URL to navigate to
                    """)
                
                self._register_tool(self._legacy_browser_click_element, "browser_click_element",
                    """Click an element on the page (legacy browser sandbox mode).
                    
                    Args:
                        index (int): Element index from the page
                        description (str): Description of the element to click
                    """)
                
                self._register_tool(self._legacy_browser_input_text, "browser_input_text",
                    """Input text into a form field (legacy browser sandbox mode).
                    
                    Args:
                        text (str): Text to input
                        index (int): Element index
                        description (str): Description of the input field
                    """)
                
                self._register_tool(self._legacy_browser_send_keys, "browser_send_keys",
                    """Send keyboard keys to the browser (legacy browser sandbox mode).
                    
                    Args:
                        keys (str): Keys to send (e.g., "Enter", "Tab")
                    """)
                
                self._register_tool(self._legacy_browser_scroll_down, "browser_scroll_down",
                    """Scroll down on the page (legacy browser sandbox mode).
                    
                    Args:
                        amount (int): Scroll amount in pixels
                    """)
                
                self._register_tool(self._legacy_browser_scroll_up, "browser_scroll_up",
                    """Scroll up on the page (legacy browser sandbox mode).
                    
                    Args:
                        amount (int): Scroll amount in pixels
                    """)
                
                self._register_tool(self._legacy_browser_go_back, "browser_go_back",
                    """Go back to the previous page (legacy browser sandbox mode).
                    """)
                
                self._register_tool(self._legacy_browser_wait, "browser_wait",
                    """Wait for a specified time (legacy browser sandbox mode).
                    
                    Args:
                        seconds (int): Time to wait in seconds
                    """)
        else:
            logger.info("[ToolkitAdapter] Registering browser_* tools in desktop compatibility mode")
            self._register_tool(self._desktop_browser_navigate_to, "browser_navigate_to",
                """Open a URL in desktop Chromium and capture the current desktop state.
                
                Args:
                    url (str): URL to open in Chromium.
                """)
            self._register_tool(self._desktop_browser_click_element, "browser_click_element",
                """Legacy compatibility wrapper. Use coordinate-based `click` for precise web interactions.
                
                Args:
                    index (int): Legacy element index (not supported in desktop mode)
                    description (str): Legacy element description (not supported in desktop mode)
                """)
            self._register_tool(self._desktop_browser_input_text, "browser_input_text",
                """Type text into the currently focused desktop element and capture state.
                
                Args:
                    text (str): Text to type.
                """)
            self._register_tool(self._desktop_browser_send_keys, "browser_send_keys",
                """Send keyboard keys in desktop mode and capture state.
                
                Args:
                    keys (str): Keys to send.
                """)
            self._register_tool(self._desktop_browser_scroll_down, "browser_scroll_down",
                """Scroll down in desktop mode and capture state.
                
                Args:
                    amount (int): Scroll amount in pixels.
                """)
            self._register_tool(self._desktop_browser_scroll_up, "browser_scroll_up",
                """Scroll up in desktop mode and capture state.
                
                Args:
                    amount (int): Scroll amount in pixels.
                """)
            self._register_tool(self._desktop_browser_go_back, "browser_go_back",
                """Use Alt+Left in desktop mode and capture state.
                """)
            self._register_tool(self._desktop_browser_wait, "browser_wait",
                """Wait in desktop mode and capture state.
                
                Args:
                    seconds (int): Time to wait in seconds.
                """)
        # === TaskListTool ===
        task_tool = self._tool_instances.get('task_list')
        if task_tool:
            self._register_tool(task_tool.create_tasks, "create_tasks",
                """Create tasks for tracking work.
                
                Args:
                    sections (list): Optional section objects, each with title and tasks fields
                    section_title (str): Optional section title for single-section task creation
                    section_id (str): Optional target section ID for single-section task creation
                    task_contents (list): Optional task list used with section_title/section_id
                """)
            
            self._register_tool(task_tool.view_tasks, "view_tasks",
                """View all tasks in the task list.
                """)
            
            self._register_tool(task_tool.update_tasks, "update_tasks",
                """Update task status or content.
                
                Args:
                    task_ids: ID(s) of the task(s) to update
                    content (str): New content for the task
                    status (str): New status (pending, completed, cancelled)
                """)
            
            self._register_tool(task_tool.delete_tasks, "delete_tasks",
                """Delete tasks from the task list.
                
                Args:
                    task_ids: ID(s) of the task(s) to delete
                    section_ids: ID(s) of the section(s) to delete
                    confirm (bool): Confirm deletion
                """)
        
        logger.info(f"[ToolkitAdapter] Registered {len(self.toolkit.get_json_schemas())} tools")

    async def _write_file_guarded(self, path: str, content: str) -> ToolResult:
        return await self._call_tool_method(
            "code",
            "write_file",
            unavailable_error="SandboxCodeTool is not available",
            path=path,
            content=content,
        )

    async def _edit_file_guarded(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        **kwargs,
    ) -> ToolResult:
        # Absorb legacy parameter names and common misspellings.
        # expected_occurences / expected_occurrences → set replace_all
        if "expected_occurences" in kwargs:
            val = kwargs.pop("expected_occurences")
            if isinstance(val, int) and val > 1:
                replace_all = True
        if "expected_occurrences" in kwargs:
            val = kwargs.pop("expected_occurrences")
            if isinstance(val, int) and val > 1:
                replace_all = True
        # start_line / end_line / new_content: absorbed for backward compat but
        # never exposed in schema — the model now uses anchor-replace only.
        start_line = kwargs.pop("start_line", None)
        end_line = kwargs.pop("end_line", None)
        new_content = kwargs.pop("new_content", None)
        instructions = kwargs.pop("instructions", "")

        # Pre-read enforcement: require read_file before edit_file.
        cache_key = str(path or "").strip()
        if cache_key:
            code_tool = self._ensure_tool_instance("code")
            if code_tool is not None:
                try:
                    cache_key = code_tool._normalize_path(cache_key)
                except (ValueError, AttributeError):
                    pass
            entry = self.file_read_cache.get(cache_key)
            if entry is None:
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "EDIT_FILE_NOT_READ",
                        "error": (
                            "File has not been read yet. Use read_file first to "
                            "see the exact file content before editing. grep, sed -n, "
                            "and cat are useful for inspection, but edit_file's "
                            "safety gate requires a read_file call on the same path. "
                            "For line-focused debugging use read_file with "
                            "line_start/line_end; offset is a byte offset, not a line number."
                        ),
                        "path": path,
                    },
                )

        # Intercept old_text == new_text BEFORE wasting a sandbox round-trip.
        # This catches character-loss no-ops where the model's old_text and
        # new_text both arrived identically (e.g. '\\frac 1 n' → '\\frac 1 n').
        if old_text == new_text:
            return ToolResult(
                success=False,
                output={
                    "error_code": "EDIT_FILE_NOOP",
                    "error": (
                        "old_text and new_text are identical — this edit "
                        "would change nothing.\n\n"
                        "If your old_text has missing characters compared to "
                        "the file (e.g. 'for in range' instead of "
                        "'for i in range', '\\\\frac 1 n' instead of "
                        "'\\\\frac{1}{n}'), single characters may have been "
                        "dropped during generation. Workaround: use "
                        "sed -i for this specific fix, or re-read the file "
                        "and copy a LONGER old_text block that includes "
                        "surrounding lines.\n"
                        f"old_text ({len(old_text)} chars): {repr(old_text[:200])}"
                    ),
                    "path": path,
                },
            )

        # Determine expected_occurrences from replace_all.
        # When replace_all is True, count occurrences in the cached read
        # so the underlying tool replaces every match.
        exp_occurrences = 1
        if replace_all:
            entry = self.file_read_cache.get(cache_key)
            if entry is not None:
                count = entry.content.count(old_text)
                if count > 1:
                    exp_occurrences = count

        result = await self._call_tool_method(
            "code",
            "edit_file",
            unavailable_error="SandboxCodeTool is not available",
            path=path,
            start_line=start_line,
            end_line=end_line,
            new_content=new_content,
            old_text=old_text,
            new_text=new_text,
            expected_occurrences=exp_occurrences,
            instructions=instructions,
        )

        # Update cache on successful edit
        if result.success and cache_key:
            updated = result.output.get("updated_content")
            if updated is not None:
                self.file_read_cache.set(cache_key, FileReadEntry(str(updated)))
            else:
                self.file_read_cache.invalidate(cache_key)

        return result

    async def _list_dir_guarded(self, path: str = ".") -> ToolResult:
        return await self._call_tool_method(
            "code",
            "list_dir",
            unavailable_error="SandboxCodeTool is not available",
            path=path,
        )

    async def _make_dir_guarded(self, path: str) -> ToolResult:
        return await self._call_tool_method(
            "code",
            "make_dir",
            unavailable_error="SandboxCodeTool is not available",
            path=path,
        )

    async def _upload_file_guarded(self, path: str, content_base64: str) -> ToolResult:
        return await self._call_tool_method(
            "code",
            "upload_file",
            unavailable_error="SandboxCodeTool is not available",
            path=path,
            content_base64=content_base64,
        )

    async def _download_file_guarded(
        self,
        path: str,
        as_base64: bool = False,
    ) -> ToolResult:
        return await self._call_tool_method(
            "code",
            "download_file",
            unavailable_error="SandboxCodeTool is not available",
            path=path,
            as_base64=as_base64,
        )

    async def _expose_port_guarded(self, port: int) -> ToolResult:
        return await self._call_tool_method(
            "code",
            "expose_port",
            unavailable_error="SandboxCodeTool is not available",
            port=port,
        )

    async def _understand_media_guarded(
        self,
        path: str,
        media_type: str = "auto",
        prompt: str = "请描述该媒体内容，提取关键信息；如有文字请转写。",
        max_output_chars: int = 4000,
        detail_level: str = "normal",
        timeout_seconds: int = 60,
    ) -> ToolResult:
        return await self._call_tool_method(
            "media",
            "understand_media",
            unavailable_error="KimiMediaUnderstandingTool is not available",
            path=path,
            media_type=media_type,
            prompt=prompt,
            max_output_chars=max_output_chars,
            detail_level=detail_level,
            timeout_seconds=timeout_seconds,
        )

    async def _web_search_tool(
        self,
        query: str,
        num_results: int = 20,
    ) -> ToolResult:
        return await self._call_tool_method(
            "web_search",
            "web_search",
            unavailable_error="SandboxWebSearchTool is not available",
            query=query,
            num_results=num_results,
        )

    async def _scrape_webpage_tool(
        self,
        urls: str | List[str],
        query: Optional[str] = None,
        chunks_per_source: Optional[int] = None,
        extract_depth: str = "basic",
        format: str = "markdown",
        include_images: bool = False,
        include_favicon: bool = False,
        timeout: Optional[float] = None,
    ) -> ToolResult:
        return await self._call_tool_method(
            "web_search",
            "scrape_webpage",
            unavailable_error="SandboxWebSearchTool is not available",
            urls=urls,
            query=query,
            chunks_per_source=chunks_per_source,
            extract_depth=extract_depth,
            format=format,
            include_images=include_images,
            include_favicon=include_favicon,
            timeout=timeout,
        )

    async def _download_images_tool(
        self,
        urls: str,
        folder_name: str = "",
    ) -> ToolResult:
        return await self._call_tool_method(
            "web_search",
            "download_images",
            unavailable_error="SandboxWebSearchTool is not available",
            urls=urls,
            folder_name=folder_name,
        )

    async def _get_available_skills_tool(self) -> ToolResult:
        return await self._call_tool_method(
            "skill",
            "get_available_skills",
            unavailable_error="SandboxSkillTool is not available",
        )

    async def _load_skill_tool(self, skill_name: str) -> ToolResult:
        return await self._call_tool_method(
            "skill",
            "load_skill",
            unavailable_error="SandboxSkillTool is not available",
            skill_name=skill_name,
        )

    async def _load_reference_tool(
        self,
        skill_name: str,
        ref_name: str,
    ) -> ToolResult:
        return await self._call_tool_method(
            "skill",
            "load_reference",
            unavailable_error="SandboxSkillTool is not available",
            skill_name=skill_name,
            ref_name=ref_name,
        )

    async def _list_skill_scripts_tool(self, skill_name: str) -> ToolResult:
        return await self._call_tool_method(
            "skill",
            "list_skill_scripts",
            unavailable_error="SandboxSkillTool is not available",
            skill_name=skill_name,
        )

    async def _run_skill_script_tool(
        self,
        skill_name: str,
        script_name: str,
        args: Optional[List[str]] = None,
    ) -> ToolResult:
        return await self._call_tool_method(
            "skill",
            "run_skill_script",
            unavailable_error="SandboxSkillTool is not available",
            skill_name=skill_name,
            script_name=script_name,
            args=args,
        )

    async def _screenshot_tool(self) -> ToolResult:
        return await self._call_tool_method(
            "computer_use",
            "screenshot",
            unavailable_error="ComputerUseTool is not available",
        )

    async def _click_tool(
        self,
        x: int,
        y: int,
        button: str = "left",
        num_clicks: int = 1,
    ) -> ToolResult:
        return await self._call_tool_method(
            "computer_use",
            "click",
            unavailable_error="ComputerUseTool is not available",
            x=x,
            y=y,
            button=button,
            num_clicks=num_clicks,
        )

    async def _typing_tool(self, text: str) -> ToolResult:
        return await self._call_tool_method(
            "computer_use",
            "typing",
            unavailable_error="ComputerUseTool is not available",
            text=text,
        )

    async def _scroll_tool(self, amount: int) -> ToolResult:
        return await self._call_tool_method(
            "computer_use",
            "scroll",
            unavailable_error="ComputerUseTool is not available",
            amount=amount,
        )

    async def _press_tool(self, keys: str) -> ToolResult:
        return await self._call_tool_method(
            "computer_use",
            "press",
            unavailable_error="ComputerUseTool is not available",
            keys=keys,
        )

    async def _move_to_tool(self, x: float, y: float) -> ToolResult:
        return await self._call_tool_method(
            "computer_use",
            "move_to",
            unavailable_error="ComputerUseTool is not available",
            x=x,
            y=y,
        )

    async def _drag_to_tool(self, x: float, y: float) -> ToolResult:
        return await self._call_tool_method(
            "computer_use",
            "drag_to",
            unavailable_error="ComputerUseTool is not available",
            x=x,
            y=y,
        )

    async def _legacy_browser_navigate_to(self, url: str) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_navigate_to",
            unavailable_error="SandboxBrowserTool is not available",
            url=url,
        )

    async def _legacy_browser_click_element(
        self,
        index: Optional[int] = None,
        description: Optional[str] = None,
    ) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_click_element",
            unavailable_error="SandboxBrowserTool is not available",
            index=index,
            description=description,
        )

    async def _legacy_browser_input_text(
        self,
        text: str,
        index: Optional[int] = None,
        description: Optional[str] = None,
    ) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_input_text",
            unavailable_error="SandboxBrowserTool is not available",
            text=text,
            index=index,
            description=description,
        )

    async def _legacy_browser_send_keys(self, keys: str) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_send_keys",
            unavailable_error="SandboxBrowserTool is not available",
            keys=keys,
        )

    async def _legacy_browser_scroll_down(self, amount: Optional[int] = None) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_scroll_down",
            unavailable_error="SandboxBrowserTool is not available",
            amount=amount,
        )

    async def _legacy_browser_scroll_up(self, amount: Optional[int] = None) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_scroll_up",
            unavailable_error="SandboxBrowserTool is not available",
            amount=amount,
        )

    async def _legacy_browser_go_back(self) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_go_back",
            unavailable_error="SandboxBrowserTool is not available",
        )

    async def _legacy_browser_wait(self, seconds: int = 3) -> ToolResult:
        return await self._call_tool_method(
            "browser",
            "browser_wait",
            unavailable_error="SandboxBrowserTool is not available",
            seconds=seconds,
        )

    def set_multimodal_guard_context(
        self,
        *,
        has_image_media_refs: bool,
        enforce_ocr_block: bool,
        image_media_refs: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Set per-run multimodal guard context."""
        self._has_image_media_refs = bool(has_image_media_refs)
        self._enforce_ocr_guard = bool(enforce_ocr_block)
        self._multimodal_uploaded_image_paths = set()

        if not self._has_image_media_refs:
            return

        for ref in image_media_refs or []:
            if not isinstance(ref, dict):
                continue
            kind = str(ref.get("kind") or "").strip().lower()
            if kind != "image":
                continue
            normalized = self._normalize_workspace_path_for_guard(str(ref.get("path") or ""))
            if normalized.startswith("/workspace/"):
                self._multimodal_uploaded_image_paths.add(normalized)

    def _normalize_workspace_path_for_guard(self, path: str) -> str:
        raw = str(path or "").strip()
        if not raw:
            return ""

        if raw.startswith("/"):
            return os.path.normpath(raw)

        return os.path.normpath(os.path.join("/workspace", raw))

    def _should_block_uploaded_image_read(self, path: str) -> tuple[bool, str]:
        if not self._enforce_ocr_guard or not self._has_image_media_refs:
            return False, ""
        if not self._multimodal_uploaded_image_paths:
            return False, ""

        normalized = self._normalize_workspace_path_for_guard(path)
        if not normalized:
            return False, normalized

        return normalized in self._multimodal_uploaded_image_paths, normalized

    def _should_block_ocr_command(self, command: str) -> bool:
        if not self._enforce_ocr_guard or not self._has_image_media_refs:
            return False

        normalized = str(command or "").lower()
        if not normalized:
            return False

        return any(re.search(pattern, normalized) for pattern in self._ocr_command_patterns)

    async def _execute_command_guarded(
        self,
        command: str,
        workdir: str = "",
        timeout: int = 1800,
        envs: Optional[Dict[str, str]] = None,
        session_name: Optional[str] = None,
        blocking: bool = True,
    ) -> ToolResult:
        code_tool = self._ensure_tool_instance("code")
        if code_tool is None:
            return ToolResult(success=False, output={"error": "SandboxCodeTool is not available"})

        kwargs = {
            "command": command,
            "workdir": workdir,
            "timeout": timeout,
            "envs": envs,
            "session_name": session_name,
            "blocking": blocking,
        }
        command = str(command or "")
        if not command.strip():
            logger.warning(
                "[ToolkitAdapter] execute_command rejected: empty command "
                "(trace_source_stage=toolkit_adapter trace_reason=empty_command)"
            )
            return ToolResult(
                success=False,
                output={
                    "error": "execute_command requires a non-empty 'command' parameter.",
                    "hint": "Specify the shell command to execute, e.g. command='ls -la'",
                    "received_command": repr(command),
                },
            )
        if self._should_block_ocr_command(command):
            safety = classify_command_safety(
                command=command,
                blocked_by_existing_guard=True,
            )
            active_run_id = self.shadow_clone_run_id or get_agent_run_context()[0]
            if active_run_id:
                record_safety_event(
                    active_run_id,
                    tool_name="execute_command",
                    safety=safety,
                )
            return ToolResult(
                success=False,
                output={
                    "error": (
                        "OCR/external image parsing is blocked for this multimodal turn. "
                        "Use native model vision understanding instead."
                    ),
                    "blocked_command": command,
                    "safety": safety,
                },
            )

        result = await code_tool.execute_command(**kwargs)
        if result.success and _is_read_only_file_inspection_command(command):
            inspected_text = ""
            if isinstance(result.output, dict):
                inspected_text = str(
                    result.output.get("stdout")
                    or result.output.get("output")
                    or "",
                )
            for inspected_path in _extract_workspace_paths_from_command(command):
                cache_key = inspected_path
                try:
                    cache_key = code_tool._normalize_path(inspected_path)
                except (ValueError, AttributeError):
                    pass
                self.file_read_cache.set(cache_key, FileReadEntry(inspected_text))
        if not result.success:
            hint = _detect_character_loss(str(result.output))
            if hint:
                output = result.output
                if isinstance(output, dict):
                    output["char_loss_hint"] = hint
        return result

    async def _read_file_guarded(
        self,
        path: str,
        max_bytes: Optional[int] = None,
        offset: int = 0,
        line_start: Optional[int] = None,
        line_end: Optional[int] = None,
    ) -> ToolResult:
        code_tool = self._ensure_tool_instance("code")
        if code_tool is None:
            return ToolResult(success=False, output={"error": "SandboxCodeTool is not available"})

        kwargs = {
            "path": path,
            "max_bytes": max_bytes,
            "offset": offset,
            "line_start": line_start,
            "line_end": line_end,
        }
        path = str(path or "")
        should_block, normalized_path = self._should_block_uploaded_image_read(path)
        if should_block:
            logger.info(
                "[ToolkitAdapter] Blocked read_file for uploaded image in multimodal turn: %s",
                normalized_path,
            )
            safety = classify_tool_violation(
                tool_name="read_file",
                blocked=True,
                blocked_reason="uploaded_image_read_blocked",
                payload={"blocked_path": normalized_path},
            )
            active_run_id = self.shadow_clone_run_id or get_agent_run_context()[0]
            if active_run_id:
                record_safety_event(
                    active_run_id,
                    tool_name="read_file",
                    safety=safety,
                )
            return ToolResult(
                success=False,
                output={
                    "error": (
                        "Reading uploaded image bytes via read_file is blocked in this multimodal turn. "
                        "Use native model vision understanding instead."
                    ),
                    "blocked_path": normalized_path,
                    "suggested_action": (
                        "Analyze the uploaded image directly with native multimodal vision; "
                        "do not parse image bytes as text."
                    ),
                    "safety": safety,
                },
            )

        result = await code_tool.read_file(**kwargs)
        if result.success and not result.output.get("is_binary"):
            normalized_path = str(result.output.get("path", path) or path)
            content = str(result.output.get("content", ""))
            if content and normalized_path:
                self.file_read_cache.set(normalized_path, FileReadEntry(content))
        return result

    async def read_file_base64(self, path: str) -> Dict[str, Any]:
        """Read a sandbox file and return a base64 payload for multimodal blocks."""
        code_tool = self._ensure_tool_instance("code")
        if code_tool is None:
            raise RuntimeError("SandboxCodeTool is not initialized")

        result = await code_tool.download_file(path=path, as_base64=True)
        if not getattr(result, "success", False):
            output = getattr(result, "output", {})
            if isinstance(output, dict):
                error = output.get("error") or "Unknown sandbox read error"
            else:
                error = str(output)
            raise RuntimeError(f"Failed to read sandbox file {path}: {error}")

        output = getattr(result, "output", {})
        if not isinstance(output, dict):
            raise RuntimeError(f"Invalid sandbox file payload for {path}")
        if not output.get("base64"):
            raise RuntimeError(f"Missing base64 payload for sandbox file {path}")
        return output

    def _safe_json_dict(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    async def _require_desktop_computer_tool(self):
        computer_tool = self._ensure_tool_instance("computer_use")
        if computer_tool is None:
            raise RuntimeError("ComputerUseTool is not initialized")
        await computer_tool._ensure_sandbox()
        return computer_tool

    async def _compose_desktop_browser_result(
        self,
        computer_tool,
        *,
        success: bool,
        message: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        screenshot_payload: Dict[str, Any] = {}
        screenshot_ok = True
        try:
            screenshot_result = await computer_tool.screenshot()
            screenshot_ok = bool(getattr(screenshot_result, 'success', True))
            screenshot_payload = self._safe_json_dict(getattr(screenshot_result, 'output', ''))
        except Exception as screenshot_error:
            screenshot_ok = False
            screenshot_payload = {
                "success": False,
                "message": f"Failed to capture desktop screenshot: {screenshot_error}",
            }

        payload: Dict[str, Any] = dict(screenshot_payload)
        payload["success"] = bool(success and screenshot_ok)
        payload["message"] = message
        payload["render_mode_hint"] = "VNC"
        payload["tool_backend"] = "desktop_computer_use"

        screenshot_info = payload.get("screenshot")
        if isinstance(screenshot_info, dict):
            screenshot_url = screenshot_info.get("url")
            if isinstance(screenshot_url, str) and screenshot_url:
                payload.setdefault("image_url", screenshot_url)

        if not payload.get("image_url"):
            base64_or_url = payload.get("base64_data")
            if isinstance(base64_or_url, str) and base64_or_url.startswith("http"):
                payload["image_url"] = base64_or_url

        if extra:
            payload.update(extra)

        return ToolResult(
            success=payload["success"],
            output=json.dumps(payload, ensure_ascii=False),
        )

    async def _desktop_browser_navigate_to(self, url: str) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        safe_url = shlex.quote(url)
        desktop_chrome_flags = (
            "--no-sandbox "
            "--disable-dev-shm-usage "
            "--disable-gpu "
            "--disable-software-rasterizer "
            "--no-first-run "
            "--no-default-browser-check "
            "--disable-background-networking "
            "--disable-background-timer-throttling "
            "--disable-renderer-backgrounding "
            "--disable-breakpad "
            "--window-size=1366,900 "
            "--user-data-dir=/tmp/chromium-desktop-profile"
        )
        launch_script = (
            "if command -v chromium >/dev/null 2>&1; then "
            f"DISPLAY=:99 nohup chromium --new-window {desktop_chrome_flags} {safe_url} >/tmp/chromium_desktop.log 2>&1 & "
            "elif command -v chromium-browser >/dev/null 2>&1; then "
            f"DISPLAY=:99 nohup chromium-browser --new-window {desktop_chrome_flags} {safe_url} >/tmp/chromium_desktop.log 2>&1 & "
            "elif command -v xdg-open >/dev/null 2>&1; then "
            f"DISPLAY=:99 nohup xdg-open {safe_url} >/tmp/chromium_desktop.log 2>&1 & "
            "else echo no_supported_browser; exit 127; fi"
        )
        launch_cmd = f"bash -lc {shlex.quote(launch_script)}"

        launch_stdout = ""
        launch_stderr = ""
        launch_success = True
        launch_exit_code = 0
        try:
            launch_result = await computer_tool._run_blocking_sandbox_call(
                "commands.run",
                lambda: computer_tool.sandbox.commands.run(launch_cmd),
            )
            launch_stdout = str(getattr(launch_result, 'stdout', '') or '')
            launch_stderr = str(getattr(launch_result, 'stderr', '') or '')
            launch_exit_code = int(getattr(launch_result, 'exit_code', 0) or 0)
            combined_output = f"{launch_stdout}\n{launch_stderr}"
            if launch_exit_code != 0 or "no_supported_browser" in combined_output:
                launch_success = False
        except Exception as launch_error:
            launch_success = False
            launch_stderr = str(launch_error)

        await asyncio.sleep(1.5)

        if launch_success:
            message = f"Opened URL in desktop browser: {url}"
        else:
            message = (
                "Desktop browser launch failed. Use computer-use tools to open Chromium manually "
                "and continue with screenshot + coordinate clicks."
            )

        return await self._compose_desktop_browser_result(
            computer_tool,
            success=launch_success,
            message=message,
            extra={
                "url": url,
                "launch_exit_code": launch_exit_code,
                "launch_stdout": launch_stdout[-500:],
                "launch_stderr": launch_stderr[-500:],
            },
        )

    async def _desktop_browser_click_element(
        self,
        index: Optional[int] = None,
        description: Optional[str] = None,
    ) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        hint = "Use screenshot + click(x, y) for precise page interactions in desktop mode."
        return await self._compose_desktop_browser_result(
            computer_tool,
            success=False,
            message=(
                "Legacy browser_click_element is disabled in desktop mode. "
                f"{hint}"
            ),
            extra={
                "legacy_index": index,
                "legacy_description": description,
                "action_required": hint,
            },
        )

    async def _desktop_browser_input_text(
        self,
        text: str,
        index: Optional[int] = None,
        description: Optional[str] = None,
    ) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        result = await computer_tool.typing(text=text)
        typed_ok = bool(getattr(result, 'success', False))
        return await self._compose_desktop_browser_result(
            computer_tool,
            success=typed_ok,
            message=(
                "Typed text via desktop keyboard into the currently focused element."
                if typed_ok
                else "Failed to type text via desktop keyboard."
            ),
            extra={
                "typed_text": text,
                "legacy_index": index,
                "legacy_description": description,
            },
        )

    async def _desktop_browser_send_keys(self, keys: str) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        result = await computer_tool.press(keys=keys)
        pressed_ok = bool(getattr(result, 'success', False))
        return await self._compose_desktop_browser_result(
            computer_tool,
            success=pressed_ok,
            message=(
                f"Sent keys via desktop keyboard: {keys}"
                if pressed_ok
                else f"Failed to send keys via desktop keyboard: {keys}"
            ),
            extra={"keys": keys},
        )

    async def _desktop_browser_scroll_down(self, amount: Optional[int] = None) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        scroll_amount = abs(int(amount if amount is not None else 800))
        result = await computer_tool.scroll(amount=scroll_amount)
        scroll_ok = bool(getattr(result, 'success', False))
        return await self._compose_desktop_browser_result(
            computer_tool,
            success=scroll_ok,
            message=(
                f"Scrolled down in desktop mode by {scroll_amount}px"
                if scroll_ok
                else f"Failed to scroll down in desktop mode by {scroll_amount}px"
            ),
            extra={"amount": scroll_amount},
        )

    async def _desktop_browser_scroll_up(self, amount: Optional[int] = None) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        scroll_amount = abs(int(amount if amount is not None else 800))
        result = await computer_tool.scroll(amount=-scroll_amount)
        scroll_ok = bool(getattr(result, 'success', False))
        return await self._compose_desktop_browser_result(
            computer_tool,
            success=scroll_ok,
            message=(
                f"Scrolled up in desktop mode by {scroll_amount}px"
                if scroll_ok
                else f"Failed to scroll up in desktop mode by {scroll_amount}px"
            ),
            extra={"amount": scroll_amount},
        )

    async def _desktop_browser_go_back(self) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        result = await computer_tool.press(keys="alt+left")
        go_back_ok = bool(getattr(result, 'success', False))
        return await self._compose_desktop_browser_result(
            computer_tool,
            success=go_back_ok,
            message=(
                "Triggered browser back navigation via Alt+Left in desktop mode"
                if go_back_ok
                else "Failed to trigger desktop back navigation (Alt+Left)"
            ),
        )

    async def _desktop_browser_wait(self, seconds: int = 3) -> ToolResult:
        computer_tool = await self._require_desktop_computer_tool()
        wait_seconds = int(seconds if seconds is not None else 3)
        result = await computer_tool.wait(seconds=wait_seconds)
        wait_ok = bool(getattr(result, 'success', False))
        return await self._compose_desktop_browser_result(
            computer_tool,
            success=wait_ok,
            message=(
                f"Waited {wait_seconds} seconds in desktop mode"
                if wait_ok
                else f"Failed to wait {wait_seconds} seconds in desktop mode"
            ),
            extra={"seconds": wait_seconds},
        )

    async def _maybe_raise_shadow_clone_fatal(
        self,
        *,
        tool_name: str,
        error: Optional[BaseException] = None,
        payload: Any = None,
    ) -> None:
        await maybe_raise_shadow_clone_fatal_tool_error(
            run_id=self.shadow_clone_run_id,
            project_id=self.project_id,
            strict_sandbox=self.shadow_clone_fail_fast,
            tool_name=tool_name,
            error=error,
            payload=payload,
        )

    @staticmethod
    def _format_shadow_clone_tool_error(error: BaseException) -> Optional[str]:
        error_code = str(getattr(error, "error_code", "") or "").strip()
        if not error_code.startswith("SHADOW_CLONE_"):
            return None

        message = str(getattr(error, "detail", "") or error)
        payload: Dict[str, Any] = {
            "message": message,
            "error_code": error_code,
            "recoverable": bool(getattr(error, "recoverable", False)),
        }
        for attr_name in (
            "run_id",
            "project_id",
            "sandbox_id",
            "binding_state",
            "root_cause_code",
            "recovery_action",
            "provider_reason",
            "provider_status_code",
            "provider_code",
            "provider_retryable",
        ):
            value = getattr(error, attr_name, None)
            if value not in (None, ""):
                payload[attr_name] = value

        normalized_message = message.strip().lower()
        root_cause_code = str(getattr(error, "root_cause_code", "") or "").strip().upper()
        is_strict_attach_refusal = (
            "strict single-sandbox mode forbids creating a replacement sandbox"
            in normalized_message
            or "refusing sandbox failover" in normalized_message
            or root_cause_code == "SHADOW_CLONE_SANDBOX_POINTER_DRIFT"
        )
        if is_strict_attach_refusal:
            payload["safety"] = classify_strict_attach_refusal(
                tool_name=str(getattr(error, "tool_name", "") or ""),
                detail=message,
                error_code=error_code,
            )
        return json.dumps(payload, ensure_ascii=False)

    def _register_tool(self, func: Callable, name: str, docstring: str):
        """
        Register a regular (non-streaming) tool.

        Args:
            func: The tool function to wrap
            name: Tool name
            docstring: Tool description
        """
        # Populate the shared required-params registry from the function
        # signature so that openrouter_model.py can access it.
        _TOOL_REQUIRED_PARAMS_REGISTRY[name] = _extract_required_params(func)

        @wraps(func)
        async def wrapper(**kwargs) -> ToolResponse:
            active_run_id = self.shadow_clone_run_id or get_agent_run_context()[0]
            try:
                # ── Pre-execution gate: check required params ──────────
                required = get_tool_required_params(name)
                if required:
                    missing = _check_missing_params(name, kwargs, required)
                    if missing:
                        logger.warning(
                            "[ToolkitAdapter] Pre-execution gate rejected "
                            "(trace_source_stage=toolkit_adapter "
                            "trace_exec_gate_reason=missing_required_params "
                            "tool=%s missing=%s)",
                            name,
                            missing,
                        )
                        return ToolResponse(
                            content=[
                                TextBlock(
                                    type="text",
                                    text=json.dumps(
                                        {
                                            "error_code": "MISSING_REQUIRED_PARAMS",
                                            "tool": name,
                                            "missing": missing,
                                            "hint": (
                                                "Provide non-empty values for: "
                                                + ", ".join(missing)
                                            ),
                                        },
                                        ensure_ascii=False,
                                    ),
                                ),
                            ],
                        )
                # ── End pre-execution gate ─────────────────────────────

                result = await func(**kwargs)
                await self._maybe_raise_shadow_clone_fatal(
                    tool_name=name,
                    payload=getattr(result, "output", None),
                )
                if active_run_id:
                    record_tool_event(
                        active_run_id,
                        tool_name=name,
                        success=bool(getattr(result, "success", False)),
                    )
                return self._convert_result(result)
            except TypeError as e:
                logger.error(
                    "[ToolkitAdapter] Malformed tool call name=%s kwargs_keys=%s error=%s",
                    name,
                    sorted(kwargs.keys()),
                    e,
                )
                return ToolResponse(
                    content=[TextBlock(type="text", text=f"Error: malformed tool call for {name}: {str(e)}")],
                )
            except ShadowCloneSandboxFatalToolError:
                raise
            except Exception as e:
                await self._maybe_raise_shadow_clone_fatal(
                    tool_name=name,
                    error=e,
                )
                if active_run_id:
                    record_tool_event(
                        active_run_id,
                        tool_name=name,
                        success=False,
                    )
                formatted_shadow_clone_error = self._format_shadow_clone_tool_error(e)
                if active_run_id and formatted_shadow_clone_error is not None:
                    try:
                        parsed_payload = json.loads(formatted_shadow_clone_error)
                    except Exception:
                        parsed_payload = {}
                    safety_payload = (
                        parsed_payload.get("safety")
                        if isinstance(parsed_payload, dict)
                        else None
                    )
                    if isinstance(safety_payload, dict):
                        record_safety_event(
                            active_run_id,
                            tool_name=name,
                            safety=safety_payload,
                        )
                logger.error(f"[ToolkitAdapter] Tool {name} failed: {e}")
                return ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                formatted_shadow_clone_error
                                if formatted_shadow_clone_error is not None
                                else f"Error: {str(e)}"
                            ),
                        )
                    ],
                )

        wrapper.__name__ = name
        wrapper.__doc__ = docstring
        try:
            wrapper.__signature__ = inspect.signature(func)
        except (TypeError, ValueError):
            pass
        self.toolkit.register_tool_function(wrapper)
        self._registered_tool_functions.append(wrapper)

        # Schema hardening: always set additionalProperties: false to
        # prevent the model from hallucinating misspelled param names like
        # expected_occurences.  Our wrappers absorb extras via **kwargs.
        # When DeepSeek strict mode is enabled, also inject strict: true
        # and clean unsupported keywords (minLength/minItems/etc.).
        tool = self.toolkit.tools.get(name)
        if tool is not None:
            params = tool.json_schema.get("function", {}).get("parameters")
            if isinstance(params, dict) and params.get("type") == "object":
                params["additionalProperties"] = False

            if os.environ.get(
                "AGENTSCOPE_DEEPSEEK_STRICT_MODE", ""
            ).strip().lower() in ("1", "true"):
                _inject_deepseek_strict_into_schema(tool.json_schema)
    
    def _register_streaming_tool(self, func: Callable, name: str, docstring: str):
        """
        Register a streaming tool (like write_file).

        Args:
            func: The tool function to wrap
            name: Tool name
            docstring: Tool description
        """
        # Populate the shared registry (same as _register_tool).
        _TOOL_REQUIRED_PARAMS_REGISTRY[name] = _extract_required_params(func)

        @wraps(func)
        async def wrapper(**kwargs) -> AsyncGenerator[ToolResponse, None]:
            active_run_id = self.shadow_clone_run_id or get_agent_run_context()[0]
            try:
                execution_kwargs = dict(kwargs)
                # For write_file, we simulate streaming progress
                if name == "write_file":
                    sanitized_kwargs = sanitize_write_file_input(kwargs)
                    content = sanitized_kwargs.get("content", "")
                    path = sanitized_kwargs.get("path", "")
                    normalized_path = path.strip() if isinstance(path, str) else ""
                    content_bytes = (
                        len(content.encode("utf-8"))
                        if isinstance(content, str)
                        else 0
                    )
                    execution_kwargs = {
                        "path": normalized_path,
                        "content": content,
                    }

                    gate_reason = None
                    gate_message = None
                    if not isinstance(path, str) or not normalized_path:
                        gate_reason = "missing_path"
                        gate_message = "write_file requires a non-empty path."
                    elif not isinstance(content, str):
                        gate_reason = "content_not_string"
                        gate_message = "write_file requires string content."
                    elif content_bytes <= 0:
                        gate_reason = "empty_content"
                        gate_message = "write_file requires non-empty content."

                    if gate_reason:
                        logger.warning(
                            "[ToolkitAdapter] write_file execution gate rejected "
                            "(trace_source_stage=toolkit_adapter trace_exec_gate_reason=%s "
                            "path=%s content_bytes=%s)",
                            gate_reason,
                            normalized_path,
                            content_bytes,
                        )
                        await self._emit_write_file_gate_rejected_status(
                            tool_func=func,
                            path=normalized_path,
                            reason=gate_reason,
                            message=gate_message or "write_file payload is incomplete.",
                            content_bytes=content_bytes,
                        )
                        gate_error = {
                            "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                            "reason": gate_reason,
                            "message": gate_message or "write_file payload is incomplete.",
                            "path": normalized_path,
                            "content_bytes": content_bytes,
                        }
                        yield ToolResponse(
                            content=[
                                TextBlock(
                                    type="text",
                                    text=json.dumps(gate_error, ensure_ascii=False),
                                ),
                            ],
                            stream=True,
                            is_last=True,
                        )
                        if active_run_id:
                            record_tool_event(
                                active_run_id,
                                tool_name=name,
                                success=False,
                            )
                        return

                    logger.info(
                        "[ToolkitAdapter] write_file execution gate passed "
                        "(trace_source_stage=toolkit_adapter trace_exec_gate_reason=ready "
                        "path=%s content_bytes=%s trace_args_hash=%s)",
                        normalized_path,
                        content_bytes,
                        hashlib.sha1(
                            f"{normalized_path}\n{content}".encode("utf-8"),
                        ).hexdigest()[:12],
                    )
                    total_bytes = content_bytes
                    
                    # Yield progress updates
                    chunk_size = max(100, total_bytes // 10)
                    for i in range(0, total_bytes, chunk_size):
                        progress = min(i + chunk_size, total_bytes)
                        yield ToolResponse(
                            content=[TextBlock(
                                type="text",
                                text=f"Writing {path}: {progress}/{total_bytes} bytes"
                            )],
                            stream=True,
                            is_last=False,
                        )
                        await asyncio.sleep(0.01)  # Small delay for streaming effect
                
                # Execute the actual function
                result = await func(**execution_kwargs)
                await self._maybe_raise_shadow_clone_fatal(
                    tool_name=name,
                    payload=getattr(result, "output", None),
                )
                if active_run_id:
                    record_tool_event(
                        active_run_id,
                        tool_name=name,
                        success=bool(getattr(result, "success", False)),
                    )
                
                # Yield final result
                yield self._convert_result(result, is_streaming=True, is_last=True)
                
            except ShadowCloneSandboxFatalToolError:
                raise
            except Exception as e:
                await self._maybe_raise_shadow_clone_fatal(
                    tool_name=name,
                    error=e,
                )
                if active_run_id:
                    record_tool_event(
                        active_run_id,
                        tool_name=name,
                        success=False,
                    )
                formatted_shadow_clone_error = self._format_shadow_clone_tool_error(e)
                if active_run_id and formatted_shadow_clone_error is not None:
                    try:
                        parsed_payload = json.loads(formatted_shadow_clone_error)
                    except Exception:
                        parsed_payload = {}
                    safety_payload = (
                        parsed_payload.get("safety")
                        if isinstance(parsed_payload, dict)
                        else None
                    )
                    if isinstance(safety_payload, dict):
                        record_safety_event(
                            active_run_id,
                            tool_name=name,
                            safety=safety_payload,
                        )
                logger.error(f"[ToolkitAdapter] Streaming tool {name} failed: {e}")
                yield ToolResponse(
                    content=[
                        TextBlock(
                            type="text",
                            text=(
                                formatted_shadow_clone_error
                                if formatted_shadow_clone_error is not None
                                else f"Error: {str(e)}"
                            ),
                        )
                    ],
                    stream=True,
                    is_last=True,
                )
        
        wrapper.__name__ = name
        wrapper.__doc__ = docstring
        try:
            wrapper.__signature__ = inspect.signature(func)
        except (TypeError, ValueError):
            pass
        self.toolkit.register_tool_function(wrapper)
        self._registered_tool_functions.append(wrapper)

        # Schema hardening: always set additionalProperties: false to
        # prevent the model from hallucinating misspelled param names like
        # expected_occurences.  Our wrappers absorb extras via **kwargs.
        # When DeepSeek strict mode is enabled, also inject strict: true
        # and clean unsupported keywords (minLength/minItems/etc.).
        tool = self.toolkit.tools.get(name)
        if tool is not None:
            params = tool.json_schema.get("function", {}).get("parameters")
            if isinstance(params, dict) and params.get("type") == "object":
                params["additionalProperties"] = False

            if os.environ.get(
                "AGENTSCOPE_DEEPSEEK_STRICT_MODE", ""
            ).strip().lower() in ("1", "true"):
                _inject_deepseek_strict_into_schema(tool.json_schema)

    def clone_toolkit(self) -> Toolkit:
        """Build a fresh Toolkit view backed by the same tool instances."""
        cloned = Toolkit()
        for registered_tool in self._registered_tool_functions:
            cloned.register_tool_function(registered_tool)
        return cloned

    def get_tool_instance(self, name: str):
        """Return the initialized tool instance by adapter key, if present."""
        return self._ensure_tool_instance(str(name or "").strip())

    async def _emit_write_file_gate_rejected_status(
        self,
        *,
        tool_func: Callable,
        path: str,
        reason: str,
        message: str,
        content_bytes: int,
    ) -> None:
        """Emit a write_file tool_failed status when execution is rejected pre-call."""
        owner = getattr(tool_func, "__self__", None)
        emitter = getattr(owner, "_emit_write_file_status", None)
        if not callable(emitter):
            code_tool = self._ensure_tool_instance("code")
            emitter = getattr(code_tool, "_emit_write_file_status", None)
        if not callable(emitter):
            return

        safe_path = path or "<missing_path>"
        try:
            await emitter(
                "tool_failed",
                safe_path,
                message=message,
                extra={
                    "exec_gate": "rejected",
                    "exec_gate_reason": reason,
                    "content_bytes": max(0, int(content_bytes)),
                    "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                },
            )
        except Exception as emit_error:
            logger.debug(
                "[ToolkitAdapter] Failed to emit write_file gate-rejection status: %s",
                emit_error,
            )
    
    def _convert_result(
        self,
        result,
        is_streaming: bool = False,
        is_last: bool = True,
    ) -> ToolResponse:
        """
        Convert a ToolResult to AgentScope ToolResponse.
        
        Args:
            result: Original ToolResult from existing tools
            is_streaming: Whether this is a streaming response
            is_last: Whether this is the last chunk
            
        Returns:
            AgentScope ToolResponse
        """
        def _stringify(value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, (bytes, bytearray)):
                return value.decode("utf-8", errors="ignore")
            if isinstance(value, str):
                return value
            try:
                return json.dumps(value, ensure_ascii=False, indent=2)
            except TypeError:
                return str(value)

        # Handle ToolResult objects
        if hasattr(result, 'output'):
            output = result.output
            if isinstance(output, dict):
                # Extract the most useful text representation
                text = output.get('output', '')
                if not text:
                    text = _stringify(output)
                else:
                    text = _stringify(text)
                    diagnostic_fields = {
                        key: output[key]
                        for key in ("error_code", "recovery_hint", "hint")
                        if output.get(key)
                    }
                    if diagnostic_fields:
                        text = (
                            f"{text}\n\n[Tool diagnostic: "
                            f"{json.dumps(diagnostic_fields, ensure_ascii=False)}]"
                        )
            else:
                text = _stringify(output)
        else:
            text = _stringify(result)
        
        # Build metadata
        metadata = {}
        if hasattr(result, 'success'):
            metadata['success'] = result.success
        if hasattr(result, 'output') and isinstance(result.output, dict):
            # Include useful fields in metadata
            for key in [
                'path',
                'bytes',
                'bytes_read',
                'total_bytes',
                'offset',
                'truncated',
                'original_length',
                'exit_code',
                'url',
            ]:
                if key in result.output:
                    metadata[key] = result.output[key]

        if self._tool_output_max_chars and len(text) > self._tool_output_max_chars:
            metadata["truncated"] = True
            metadata["original_length"] = len(text)
            truncated = text[: self._tool_output_max_chars]
            text = (
                truncated
                + f"...<truncated {len(text) - self._tool_output_max_chars} chars>"
            )
        
        return ToolResponse(
            content=[TextBlock(type="text", text=text)],
            stream=is_streaming,
            is_last=is_last,
            metadata=metadata if metadata else None,
        )
    
    def get_toolkit(self) -> Toolkit:
        """Get the configured AgentScope Toolkit."""
        return self.toolkit
    
    def get_tool_schemas(self) -> list:
        """Get JSON schemas for all registered tools."""
        return self.toolkit.get_json_schemas()
