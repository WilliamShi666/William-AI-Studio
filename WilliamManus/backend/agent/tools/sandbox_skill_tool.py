from __future__ import annotations

import json
import os
import shlex
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from agentpress.tool import ToolResult
from sandbox.tool_base import SandboxToolsBase
from utils.logger import logger

if TYPE_CHECKING:
    from agentpress.adk_thread_manager import ADKThreadManager


MAX_OUTPUT_CHARS = 8000
SKILL_REFERENCE_MAX_CHARS = int(os.getenv("AGENTSCOPE_SKILL_REFERENCE_MAX_CHARS", "15000"))
SKILLS_WORKSPACE_DIR = "/workspace/skills"
BOOTSTRAP_TARGET = "/workspace/claude_skills_sandbox_bootstrap.py"
BOOTSTRAP_MODE = "copy"
BOOTSTRAP_METADATA = f"{SKILLS_WORKSPACE_DIR}/skills_metadata.json"


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if text is None:
        return ""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode("utf-8", errors="ignore")
    if not isinstance(text, str):
        text = str(text)
    if len(text) > limit:
        return text[:limit] + f"...<truncated {len(text) - limit} chars>"
    return text


def _strip_frontmatter(content: str) -> str:
    if content.startswith("---"):
        parts = content.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip()
    return content


class SandboxSkillTool(SandboxToolsBase):
    """Claude Skills toolset backed by the PPIO sandbox (/workspace/skills)."""

    def __init__(
        self,
        project_id: str,
        thread_manager: Optional["ADKThreadManager"] = None,
        *,
        shadow_clone_run_id: Optional[str] = None,
        strict_sandbox: bool = False,
    ):
        super().__init__(
            project_id=project_id,
            thread_manager=thread_manager,
            sandbox_type="code",
            shadow_clone_run_id=shadow_clone_run_id,
            strict_sandbox=strict_sandbox,
        )

    async def _run_command(self, command: str):
        return await self._run_blocking_sandbox_call(
            "commands.run",
            lambda: self.sandbox.commands.run(command),
        )

    async def _read_file(self, path: str):
        return await self._run_blocking_sandbox_call(
            "files.read",
            lambda: self.sandbox.files.read(path),
        )

    async def _write_file(self, path: str, content: Any) -> None:
        await self._run_blocking_sandbox_call(
            "files.write",
            lambda: self.sandbox.files.write(path, content),
        )
        await self._persist_workspace_artifact(
            path,
            content,
            source="sandbox_skill_tool.write_file",
        )

    async def _file_exists(self, path: str) -> bool:
        cmd = f"test -f {shlex.quote(path)} && echo OK || echo MISSING"
        result = await self._run_command(cmd)
        stdout = getattr(result, "stdout", "")
        return "OK" in stdout

    async def _read_json(self, path: str) -> Optional[Dict[str, Any]]:
        try:
            content = await self._read_file(path)
            if isinstance(content, (bytes, bytearray)):
                content = content.decode("utf-8", errors="ignore")
            return json.loads(content)
        except Exception as e:
            logger.warning(f"Failed to read JSON at {path}: {e}")
            return None

    async def _ensure_skills_ready(self) -> None:
        await self._ensure_sandbox()
        try:
            await self._ensure_claude_skills_runtime_ready(force=True)
        except Exception as e:
            logger.warning(f"Skills bootstrap failed: {e}")
            raise

    async def _load_metadata(self) -> Dict[str, Any]:
        await self._ensure_skills_ready()
        data = await self._read_json(BOOTSTRAP_METADATA)
        if not data:
            raise RuntimeError("skills_metadata.json missing or invalid")
        return data

    def _resolve_skill_dir(self, metadata: Dict[str, Any], skill_name: str) -> Optional[str]:
        normalized = skill_name.strip()
        for skill in metadata.get("skills", []):
            if normalized in (skill.get("name"), skill.get("dir_name")):
                return skill.get("dir_name") or skill.get("name")
        return None

    async def get_available_skills(self) -> ToolResult:
        """List available skills (name + description) from /workspace/skills."""
        try:
            metadata = await self._load_metadata()
            skills = []
            for skill in metadata.get("skills", []):
                skills.append(
                    {
                        "name": skill.get("name") or skill.get("dir_name"),
                        "description": skill.get("description", ""),
                    }
                )
            return ToolResult(success=True, output={"skills": skills, "output": skills})
        except Exception as e:
            logger.warning(f"get_available_skills failed: {e}")
            return ToolResult(success=False, output={"error": str(e)})

    async def load_skill(self, skill_name: str) -> ToolResult:
        """Load the SKILL.md body (without frontmatter)."""
        try:
            metadata = await self._load_metadata()
            skill_dir = self._resolve_skill_dir(metadata, skill_name)
            if not skill_dir:
                return ToolResult(success=False, output={"error": f"Unknown skill: {skill_name}"})

            path = f"{SKILLS_WORKSPACE_DIR}/{skill_dir}/SKILL.md"
            content = await self._read_file(path)
            if isinstance(content, (bytes, bytearray)):
                content = content.decode("utf-8", errors="ignore")
            body = _strip_frontmatter(content)
            return ToolResult(success=True, output={"skill": skill_name, "content": body, "output": body})
        except Exception as e:
            logger.warning(f"load_skill failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "skill": skill_name})

    async def load_reference(self, skill_name: str, ref_name: str) -> ToolResult:
        """Load a reference doc from a skill (references/ or ooxml/)."""
        try:
            metadata = await self._load_metadata()
            skill_dir = self._resolve_skill_dir(metadata, skill_name)
            if not skill_dir:
                return ToolResult(success=False, output={"error": f"Unknown skill: {skill_name}"})

            candidates = [
                f"{SKILLS_WORKSPACE_DIR}/{skill_dir}/{ref_name}",
                f"{SKILLS_WORKSPACE_DIR}/{skill_dir}/references/{ref_name}",
                f"{SKILLS_WORKSPACE_DIR}/{skill_dir}/ooxml/{ref_name}",
            ]

            for path in candidates:
                try:
                    content = await self._read_file(path)
                except Exception:
                    continue
                if isinstance(content, (bytes, bytearray)):
                    content = content.decode("utf-8", errors="ignore")
                original_length = len(content)
                if original_length > SKILL_REFERENCE_MAX_CHARS:
                    content = (
                        content[:SKILL_REFERENCE_MAX_CHARS]
                        + "\n\n... (content truncated)"
                    )
                return ToolResult(
                    success=True,
                    output={
                        "skill": skill_name,
                        "reference": ref_name,
                        "content": content,
                        "output": content,
                        "truncated": original_length > SKILL_REFERENCE_MAX_CHARS,
                        "original_length": original_length,
                    },
                )

            return ToolResult(success=False, output={"error": f"Reference not found: {ref_name}"})
        except Exception as e:
            logger.warning(f"load_reference failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "skill": skill_name})

    async def list_skill_scripts(self, skill_name: str) -> ToolResult:
        """List available scripts inside a skill directory."""
        try:
            metadata = await self._load_metadata()
            skill_dir = self._resolve_skill_dir(metadata, skill_name)
            if not skill_dir:
                return ToolResult(success=False, output={"error": f"Unknown skill: {skill_name}"})

            base = f"{SKILLS_WORKSPACE_DIR}/{skill_dir}"
            cmd = (
                f"find {shlex.quote(base)} -maxdepth 5 -type f "
                f"\\( -name '*.py' -o -name '*.js' -o -name '*.sh' \\)"
            )
            result = await self._run_command(cmd)
            stdout = getattr(result, "stdout", "")
            scripts: List[Dict[str, str]] = []
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                name = os.path.basename(line)
                if name.startswith("__") or name.endswith("_test.py"):
                    continue
                rel = os.path.relpath(line, base)
                ext = os.path.splitext(name)[1].lstrip(".")
                scripts.append({"name": name, "path": rel, "type": ext})

            return ToolResult(success=True, output={"skill": skill_name, "scripts": scripts, "output": scripts})
        except Exception as e:
            logger.warning(f"list_skill_scripts failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "skill": skill_name})

    async def run_skill_code(self, code: str) -> ToolResult:
        """Execute Python code inside the sandbox."""
        try:
            await self._ensure_skills_ready()
            run_code_error = None
            if hasattr(self.sandbox, "run_code"):
                try:
                    result = await self._run_blocking_sandbox_call(
                        "run_code",
                        lambda: self.sandbox.run_code(code, language="python"),
                    )
                    payload = {
                        "logs": _truncate(getattr(result, "logs", "")),
                        "result": _truncate(getattr(result, "result", "")),
                        "error": getattr(result, "error", "") or None,
                    }
                    payload["output"] = payload["logs"] or payload["result"] or ""
                    success = payload["error"] in (None, "", 0)
                    if success:
                        return ToolResult(success=True, output=payload)
                    run_code_error = payload["error"] or "run_code failed"
                except Exception as e:
                    run_code_error = str(e)

            temp_path = "/workspace/_temp_skill_code.py"
            await self._write_file(temp_path, code)
            result = await self._run_command(f"python3 {shlex.quote(temp_path)}")
            payload = {
                "stdout": _truncate(getattr(result, "stdout", "")),
                "stderr": _truncate(getattr(result, "stderr", "")),
                "exit_code": getattr(result, "exit_code", 0),
                "run_code_error": run_code_error,
            }
            payload["output"] = payload["stdout"] or payload["stderr"] or ""
            return ToolResult(success=payload["exit_code"] == 0, output=payload)
        except Exception as e:
            logger.warning(f"run_skill_code failed: {e}")
            return ToolResult(success=False, output={"error": str(e)})

    async def run_skill_script(self, skill_name: str, script_name: str, args: Optional[List[str]] = None) -> ToolResult:
        """Run a skill script inside the sandbox."""
        try:
            metadata = await self._load_metadata()
            skill_dir = self._resolve_skill_dir(metadata, skill_name)
            if not skill_dir:
                return ToolResult(success=False, output={"error": f"Unknown skill: {skill_name}"})

            base = f"{SKILLS_WORKSPACE_DIR}/{skill_dir}"
            candidates = [script_name]
            if "/" not in script_name:
                candidates = [
                    script_name,
                    f"scripts/{script_name}",
                    f"ooxml/scripts/{script_name}",
                ]

            script_path = None
            for rel in candidates:
                path = f"{base}/{rel}"
                if await self._file_exists(path):
                    script_path = rel
                    break

            if not script_path:
                return ToolResult(success=False, output={"error": f"Script not found: {script_name}"})

            ext = os.path.splitext(script_path)[1].lstrip(".")
            runner = {"py": "python3", "js": "node", "sh": "bash"}.get(ext)
            if not runner:
                return ToolResult(success=False, output={"error": f"Unsupported script type: .{ext}"})

            arg_list = args or []
            arg_str = " ".join(shlex.quote(str(arg)) for arg in arg_list)
            env_prefix = ""
            if runner == "node":
                env_prefix = "export NODE_PATH=$(npm root -g) && "
            cmd = f"cd {shlex.quote(base)} && {env_prefix}{runner} {shlex.quote(script_path)} {arg_str}".strip()

            result = await self._run_command(cmd)
            payload = {
                "command": cmd,
                "stdout": _truncate(getattr(result, "stdout", "")),
                "stderr": _truncate(getattr(result, "stderr", "")),
                "exit_code": getattr(result, "exit_code", 0),
            }
            payload["output"] = payload["stdout"] or payload["stderr"] or ""
            return ToolResult(success=payload["exit_code"] == 0, output=payload)
        except Exception as e:
            logger.warning(f"run_skill_script failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "skill": skill_name, "script": script_name})
