"""
SandboxClaudeBridge — E2B communication layer for Claude Agent SDK.

Connects ClaudeSDKRunner to the Claude Agent Service running inside a
PPIO sandbox. Uses E2B SDK to create the sandbox, send prompts, and
stream JSON-line responses back to the runner.

Architecture:
  ClaudeSDKRunner  ──>  SandboxClaudeBridge  ──>  Sandbox (claude_agent_service.py)
       │                        │                           │
       │  bridge.start()        │  sandbox.commands.run()   │
       │  bridge.read_stream()  │  stdout JSON lines        │
       │  bridge.cleanup()      │  sandbox.kill()           │
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import shlex
import shutil
from pathlib import Path
from typing import Any, AsyncGenerator, AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)

def _resolve_local_workspace_target(cwd: str, workspace_path: str) -> Path:
    """Map an absolute /workspace path into cwd without allowing path escape."""
    normalized_path = os.path.normpath(str(workspace_path or ""))
    if normalized_path != "/workspace" and not normalized_path.startswith("/workspace/"):
        raise ValueError(f"Workspace path must stay under /workspace: {workspace_path}")
    relative_path = os.path.relpath(normalized_path, "/workspace")
    base = Path(cwd).resolve()
    target = (base / relative_path).resolve()
    if target != base and base not in target.parents:
        raise ValueError(f"Workspace path escapes local workspace: {workspace_path}")
    return target

DEFAULT_CLAUDE_TEMPLATE = "claude-agent-v1"
DEFAULT_CLAUDE_TEMPLATE_ID = ""  # PPIO-assigned after first push

_SANDBOX_STDOUT_PATH = "/tmp/claude_output.jsonl"
_SANDBOX_STDERR_PATH = "/tmp/claude_error.log"
_SANDBOX_DONE_PATH = "/tmp/claude_done"
_SANDBOX_EXIT_CODE_PATH = "/tmp/claude_exit_code"
_SANDBOX_REQUEST_PATH = "/tmp/claude_request.json"
_SANDBOX_CLAUDE_PATH = "/opt/npm-global/bin:/usr/local/bin:/usr/bin:/bin"


class SandboxClaudeBridge:
    """Manages a Claude Agent sandbox and streams responses from it.

    Usage:
        bridge = SandboxClaudeBridge(
            deepseek_api_key=...,
            template_id=...,
        )
        await bridge.start(prompt="...", thread_id="...")
        async for msg in bridge.read_stream():
            print(msg)
        await bridge.cleanup()
    """

    def __init__(
        self,
        *,
        deepseek_api_key: str,
        template_id: str = DEFAULT_CLAUDE_TEMPLATE_ID,
        template_name: str = DEFAULT_CLAUDE_TEMPLATE,
        sandbox_timeout: int = 3_600,
        model: str = "deepseek-v4-pro[1m]",
        system_prompt: str = "",
        mcp_servers: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.workspace_dir = "/workspace"
        self._deepseek_api_key = deepseek_api_key
        self._template_id = template_id or DEFAULT_CLAUDE_TEMPLATE_ID
        self._template_name = template_name
        self._sandbox_timeout = sandbox_timeout
        self._model = model
        self._system_prompt = system_prompt
        self._mcp_servers = mcp_servers
        self._sandbox = None
        self._process = None
        self.sandbox_id = ""

    async def start(
        self,
        prompt: str,
        thread_id: str = "",
        system_prompt: str = "",
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create sandbox and launch the Claude Agent Service.

        Sends the initial prompt request via stdin. Returns "ok" on success.
        """
        if not self._deepseek_api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is required for Claude SDK bridge")

        # Merge env vars for the sandbox
        sandbox_envs: Dict[str, str] = {
            "ANTHROPIC_AUTH_TOKEN": self._deepseek_api_key,
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_MODEL": self._model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-pro",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
            "PATH": _SANDBOX_CLAUDE_PATH,
        }
        if env:
            sandbox_envs.update(env)

        effective_system_prompt = system_prompt or self._system_prompt

        # Build the JSON request for the Claude Agent Service
        request = {
            "prompt": prompt,
            "thread_id": thread_id,
            "system_prompt": effective_system_prompt,
            "model": self._model,
            "env": sandbox_envs,
            "mcp_servers": self._mcp_servers,
        }
        request_json = json.dumps(request, ensure_ascii=False)

        # Create sandbox with Claude Agent template
        template_ref = self._template_id or self._template_name
        try:
            self._sandbox = await self._create_sandbox(
                template_ref=template_ref,
                timeout=self._sandbox_timeout,
                envs=sandbox_envs,
            )
            self.sandbox_id = str(getattr(self._sandbox, "sandbox_id", "") or "")

            await self._prepare_workspace_permissions()
            await self._write_sandbox_text(_SANDBOX_REQUEST_PATH, request_json)

            # Run the Claude Agent Service, pipe request via stdin
            self._process = await self._run_sandbox_command(
                self._service_command(),
                background=True,
            )
        except Exception as exc:
            logger.error("[SandboxClaudeBridge] Failed to start: %s", exc)
            await self._safe_cleanup()
            raise

        return "ok"


    @staticmethod
    def _service_command() -> str:
        return (
            "rm -f /tmp/claude_output.jsonl /tmp/claude_error.log "
            "/tmp/claude_done /tmp/claude_exit_code; "
            "sh -c 'PATH=/opt/npm-global/bin:/usr/local/bin:/usr/bin:/bin "
            "python3 /opt/claude_agent_service.py "
            "< /tmp/claude_request.json "
            "> /tmp/claude_output.jsonl 2> /tmp/claude_error.log; "
            "code=$?; echo $code > /tmp/claude_exit_code; "
            "touch /tmp/claude_done'"
        )

    async def read_stream(self, poll_interval_seconds: float = 0.25, timeout_seconds: float = 600) -> AsyncGenerator[Dict[str, Any], None]:
        """Poll sandbox JSONL output and yield parsed SDK messages.

        PPIO/E2B background command stdout is not exposed as a Python async
        iterator in this bridge, so start() redirects service output to a
        JSONL file. This method tails that file by byte offset, yielding only
        complete new JSON lines until a terminal result is seen, the service
        writes its done marker, or timeout expires.
        """
        if self._process is None or self._sandbox is None:
            return

        deadline = asyncio.get_event_loop().time() + timeout_seconds
        offset = 0
        pending_fragment = ""

        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                stderr = await self._read_sandbox_text(_SANDBOX_STDERR_PATH)
                detail = stderr.strip() or f"Sandbox execution timed out after {timeout_seconds:g} seconds"
                yield {
                    "type": "result",
                    "subtype": "error_during_execution",
                    "is_error": True,
                    "result": detail,
                }
                return

            raw_output = await self._read_sandbox_text(_SANDBOX_STDOUT_PATH)
            if len(raw_output) < offset:
                offset = 0
                pending_fragment = ""

            if len(raw_output) > offset:
                chunk = pending_fragment + raw_output[offset:]
                offset = len(raw_output)
                lines = chunk.splitlines(keepends=True)
                pending_fragment = ""
                for line in lines:
                    if not line.endswith(("\n", "\r")):
                        pending_fragment = line
                        continue
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        message = json.loads(line_str)
                    except json.JSONDecodeError:
                        logger.warning(
                            "[SandboxClaudeBridge] Skipping malformed JSONL line: %s",
                            line_str[:200],
                        )
                        continue
                    yield message
                    if message.get("type") == "result":
                        return

            done = (await self._read_sandbox_text(_SANDBOX_DONE_PATH)).strip()
            if done:
                if pending_fragment.strip():
                    try:
                        message = json.loads(pending_fragment.strip())
                        yield message
                        if message.get("type") == "result":
                            return
                    except json.JSONDecodeError:
                        pass
                stderr = await self._read_sandbox_text(_SANDBOX_STDERR_PATH)
                exit_code = (await self._read_sandbox_text(_SANDBOX_EXIT_CODE_PATH)).strip()
                if exit_code and exit_code != "0":
                    yield {
                        "type": "result",
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "result": stderr.strip() or f"Sandbox service exited with code {exit_code}",
                    }
                return

            await asyncio.sleep(max(0.001, poll_interval_seconds))


    async def _prepare_workspace_permissions(self) -> None:
        """Ensure the non-root Claude process can write user-visible workspace files."""
        await self._run_sandbox_command(
            "mkdir -p /workspace && "
            "(chown -R user:user /workspace 2>/dev/null || chmod 777 /workspace)",
            user="root",
        )

    async def _read_sandbox_text(self, path: str) -> str:
        if self._sandbox is None:
            return ""
        files = getattr(self._sandbox, "files", None)
        if files is not None and hasattr(files, "read"):
            try:
                read_result = files.read(path)
                if inspect.isawaitable(read_result):
                    read_result = await read_result
                return str(read_result or "")
            except Exception as exc:
                logger.debug("[SandboxClaudeBridge] Failed reading file %s: %s", path, exc)
        command = f"cat {path} 2>/dev/null || true"
        try:
            result = await asyncio.wait_for(self._run_sandbox_command(command), timeout=5.0)
        except Exception as exc:
            logger.debug("[SandboxClaudeBridge] Failed reading %s: %s", path, exc)
            return ""
        if isinstance(result, str):
            return result
        stdout = getattr(result, "stdout", None)
        if stdout is not None:
            return str(stdout)
        return str(result or "")

    async def list_workspace_files(self, root: str = "/workspace") -> list[dict[str, Any]]:
        """Return visible workspace file entries from the sandbox filesystem."""
        if self._sandbox is None:
            return []
        files = getattr(self._sandbox, "files", None)
        if files is None or not hasattr(files, "list"):
            return []
        try:
            list_result = files.list(root, depth=20)
            if inspect.isawaitable(list_result):
                list_result = await list_result
            if list_result is None:
                return []
            entries: list[dict[str, Any]] = []
            for entry in list_result:
                if isinstance(entry, dict):
                    entry_type = entry.get("type")
                    entry_path = entry.get("path")
                    entry_size = entry.get("size")
                else:
                    entry_type = getattr(entry, "type", None)
                    entry_path = getattr(entry, "path", None)
                    entry_size = getattr(entry, "size", None)
                entry_type_value = getattr(entry_type, "value", entry_type)
                if str(entry_type_value).lower() != "file":
                    continue
                entries.append({
                    "path": str(entry_path or ""),
                    "type": "file",
                    "size": entry_size,
                })
            return entries
        except Exception as exc:
            logger.debug("[SandboxClaudeBridge] Failed listing workspace files under %s: %s", root, exc)
            return []

    async def read_workspace_file_bytes(self, path: str) -> bytes:
        """Read a sandbox workspace file as bytes."""
        if self._sandbox is None:
            raise RuntimeError("Sandbox is not initialized")
        files = getattr(self._sandbox, "files", None)
        if files is None or not hasattr(files, "read"):
            raise RuntimeError("Sandbox files API is unavailable")
        read_result = files.read(path, format="bytes")
        if inspect.isawaitable(read_result):
            read_result = await read_result
        if isinstance(read_result, (bytes, bytearray)):
            return bytes(read_result)
        if isinstance(read_result, str):
            return read_result.encode("utf-8")
        return bytes(read_result or b"")

    async def make_workspace_dir(self, path: str) -> None:
        """Create a directory in the sandbox workspace."""
        if self._sandbox is None:
            raise RuntimeError("Sandbox is not initialized")
        files = getattr(self._sandbox, "files", None)
        if files is not None and hasattr(files, "make_dir"):
            result = files.make_dir(path)
            if inspect.isawaitable(result):
                await result
            return
        await self._run_sandbox_command(f"mkdir -p {shlex.quote(path)}")

    async def write_workspace_file_bytes(self, path: str, data: bytes) -> None:
        """Write bytes to a sandbox workspace file."""
        if self._sandbox is None:
            raise RuntimeError("Sandbox is not initialized")
        parent = os.path.dirname(path)
        if parent:
            await self.make_workspace_dir(parent)
        files = getattr(self._sandbox, "files", None)
        if files is not None and hasattr(files, "write"):
            result = files.write(path, data)
            if inspect.isawaitable(result):
                await result
            return
        command = f"cat > {shlex.quote(path)}"
        await self._run_sandbox_command(command, stdin=data)

    async def _write_sandbox_text(self, path: str, data: str) -> None:
        if self._sandbox is None:
            raise RuntimeError("Sandbox is not initialized")
        files = getattr(self._sandbox, "files", None)
        if files is not None and hasattr(files, "write"):
            write_result = files.write(path, data)
            if inspect.isawaitable(write_result):
                await write_result
            return
        command = f"cat > {path}"
        await self._run_sandbox_command(command, stdin=data)

    async def _run_sandbox_command(self, command: str, **kwargs: Any) -> Any:
        if self._sandbox is None:
            raise RuntimeError("Sandbox is not initialized")
        run = self._sandbox.commands.run
        effective_kwargs = dict(kwargs)
        if inspect.iscoroutinefunction(run):
            try:
                return await run(command, **effective_kwargs)
            except TypeError:
                effective_kwargs.pop("stdin", None)
                return await run(command, **effective_kwargs)

        try:
            return run(command, **effective_kwargs)
        except TypeError:
            fallback_kwargs = {
                key: value for key, value in effective_kwargs.items() if key != "stdin"
            }
            return run(command, **fallback_kwargs)

    async def cleanup(self) -> None:
        """Kill the sandbox, freeing resources."""
        await self._safe_cleanup()

    async def _safe_cleanup(self) -> None:
        if self._process is not None:
            try:
                result = self._process.kill()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
            self._process = None

        if self._sandbox is not None:
            try:
                result = self._sandbox.kill()
                if inspect.isawaitable(result):
                    await result
            except Exception:
                pass
            self._sandbox = None
            self.sandbox_id = ""

    async def _create_sandbox(
        self,
        template_ref: str,
        timeout: int,
        envs: Dict[str, str],
    ):
        """Create a sandbox using the E2B/PPIO SDK.

        For production, this imports and uses the E2B Sandbox class.
        For development/staging, raises NotImplementedError if SDK unavailable.
        """
        try:
            from e2b_code_interpreter import Sandbox as E2BSandbox
        except ImportError:
            try:
                from ppio_sandbox.core import Sandbox as E2BSandbox
            except ImportError:
                raise RuntimeError(
                    "Neither e2b_code_interpreter nor ppio_sandbox is installed. "
                    "Cannot create Claude sandbox."
                )

        metadata = {
            "sandbox_type": "code",
            "template_id": str(template_ref),
            "project_type": "claude_agent",
            "version": "1.0",
        }

        create = getattr(E2BSandbox, "create", None)
        factory = create or E2BSandbox
        sandbox = factory(
            template=template_ref,
            timeout=timeout,
            metadata=metadata,
            envs=envs if envs else None,
        )
        if inspect.isawaitable(sandbox):
            return await sandbox
        return sandbox


class FakeClaudeBridge:
    """In-process bridge for testing without a real sandbox."""

    def __init__(self, messages=None):
        self._messages = list(messages or [])
        self._started = False
        self._cleaned_up = False

    async def start(self, prompt, thread_id=None, system_prompt=None, env=None):
        self._started = True
        return "ok"

    async def read_stream(self):
        for msg in self._messages:
            yield msg

    async def cleanup(self):
        self._cleaned_up = True


class LocalClaudeBridge:
    """Runs claude_agent_service.py as a local subprocess (no sandbox).

    For development/testing without a PPIO sandbox. Set CLAUDE_LOCAL_MODE=1
    to use this bridge instead of SandboxClaudeBridge.
    """

    def __init__(
        self,
        *,
        deepseek_api_key: str,
        model: str = "deepseek-v4-pro",
        system_prompt: str = "",
        cwd: str = "/tmp/claude-test-workspace",
        service_path: str = "",
    ) -> None:
        import os as _os
        self._api_key = deepseek_api_key
        self._model = model
        self._system_prompt = system_prompt
        self._cwd = cwd
        self.workspace_dir = cwd
        self._process = None
        self._stdout_queue: asyncio.Queue = asyncio.Queue()
        self._reader_task = None
        self._service_path = service_path or _os.path.join(
            _os.path.dirname(__file__), "claude_agent_service.py"
        )

    async def start(
        self,
        prompt: str,
        thread_id: str = "",
        system_prompt: str = "",
        env=None,
    ) -> str:
        os.makedirs(self._cwd, mode=0o700, exist_ok=True)
        try:
            os.chmod(self._cwd, 0o700)
        except OSError:
            logger.debug("[LocalClaudeBridge] Could not chmod local workspace: %s", self._cwd)
        self._ensure_local_skills_link(cwd=self._cwd)

        sandbox_envs = {
            "ANTHROPIC_AUTH_TOKEN": self._api_key,
            "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic",
            "ANTHROPIC_MODEL": self._model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-pro",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
            "CLAUDE_CODE_EFFORT_LEVEL": "max",
        }
        if env:
            sandbox_envs.update(env)

        request = {
            "prompt": prompt,
            "thread_id": thread_id,
            "system_prompt": self._build_local_system_prompt(system_prompt or self._system_prompt),
            "model": self._model,
            "cwd": self._service_cwd(),
            "permission_mode": self._service_permission_mode(),
            "env": sandbox_envs,
        }
        request_json = json.dumps(request, ensure_ascii=False)

        command = self._service_command()
        process_env = self._service_process_env()
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=process_env,
        )

        # Write request to stdin and close it
        if self._process.stdin:
            self._process.stdin.write(request_json.encode())
            await self._process.stdin.drain()
            self._process.stdin.close()

        return "ok"

    def _should_bind_workspace(self) -> bool:
        """Whether to bind this run cwd to absolute /workspace for local mode."""
        flag = os.getenv("CLAUDE_LOCAL_BIND_WORKSPACE", "1").strip().lower()
        return flag not in {"0", "false", "no", "off"}

    def _allow_unisolated_fallback(self) -> bool:
        flag = os.getenv("CLAUDE_LOCAL_ALLOW_UNISOLATED_FALLBACK", "").strip().lower()
        return flag in {"1", "true", "yes", "on"}

    def _can_bind_workspace(self) -> bool:
        return self._should_bind_workspace() and bool(shutil.which("unshare"))

    def _service_cwd(self) -> str:
        """Return the cwd visible to Claude Code inside the service process."""
        return "/workspace" if self._can_bind_workspace() else self._cwd

    def _service_permission_mode(self) -> str:
        """Return permission mode compatible with the local service wrapper.

        The user-namespace bind mount maps the service to root inside the
        namespace. Claude Code refuses bypassPermissions as root, so use
        acceptEdits for this local wrapper. This is a compatibility choice;
        isolation comes from per-run cwd, private mount namespace, and
        run-scoped artifact filtering.
        """
        if self._should_bind_workspace():
            return "acceptEdits"
        return "bypassPermissions"

    def _service_process_env(self) -> Dict[str, str]:
        env = dict(os.environ)
        env["CLAUDE_LOCAL_WORKSPACE_DIR"] = self._cwd
        env["CLAUDE_LOCAL_WORKSPACE_ROOT"] = self._namespace_workspace_root()
        env["CLAUDE_AGENT_SERVICE_PATH"] = self._service_path
        return env

    def _namespace_workspace_root(self) -> str:
        workspace = Path(self._cwd).resolve()
        if workspace.parent.name == "runs" and workspace.parent.parent != workspace.anchor:
            return str(workspace.parent.parent)
        return str(workspace)

    def _service_command(self) -> tuple[str, ...]:
        """Command used to launch the local Claude Agent service.

        Local mode must isolate both relative cwd writes and absolute
        /workspace writes. Claude Code may legitimately use absolute paths when
        the user asks for /workspace/foo, so launch the service in a private
        mount namespace and bind-mount this run directory onto /workspace.
        """
        if not self._can_bind_workspace():
            if self._should_bind_workspace() and not self._allow_unisolated_fallback():
                raise RuntimeError(
                    "CLAUDE_LOCAL_BIND_WORKSPACE is enabled but unshare is not available; "
                    "refusing to run local Claude SDK without /workspace isolation"
                )
            logger.warning(
                "[LocalClaudeBridge] Running without /workspace mount isolation; "
                "this should only be used for local development"
            )
            return ("python3", self._service_path)

        script = (
            'set -e; '
            'mount --bind "$CLAUDE_LOCAL_WORKSPACE_DIR" /workspace; '
            'mount -t tmpfs -o size=64m,mode=700 tmpfs "$CLAUDE_LOCAL_WORKSPACE_ROOT"; '
            'mkdir -p /tmp/claude-test-workspace; '
            'mount -t tmpfs -o size=64m,mode=700 tmpfs /tmp/claude-test-workspace; '
            'cd /workspace; '
            'exec python3 "$CLAUDE_AGENT_SERVICE_PATH"'
        )
        return ("unshare", "--user", "--map-root-user", "--mount", "bash", "-lc", script)


    def _build_local_system_prompt(self, system_prompt: str = "") -> str:
        """Add local-mode path guidance so /workspace maps to the run cwd.

        Claude Code receives an arbitrary per-run cwd in local mode, not a
        real /workspace mount. The product/UI still exposes files as
        /workspace/<relative path>, so nudge the agent to write relative paths
        under the current working directory whenever the user asks for
        /workspace paths.
        """
        guidance = (
            "Local workspace path mapping: the current working directory is "
            "the user-visible /workspace root for this run. If the user asks "
            "for /workspace/<name>, create or edit the relative path <name> "
            "inside the current working directory. Do not use the host-level "
            "/tmp workspace path in user-facing replies."
        )
        base = str(system_prompt or "").strip()
        if base:
            return f"{base}\n\n{guidance}"
        return guidance

    @staticmethod
    def _ensure_local_skills_link(
        *,
        cwd: str,
        skills_source: Optional[Path] = None,
    ) -> None:
        """Expose AgentScope/Claude skills as project Claude Code skills locally.

        The Claude Agent SDK discovers project Skills under
        ``<cwd>/.claude/skills`` when ``setting_sources`` includes ``project``.
        The production sandbox template creates this link at image-build time;
        local mode uses an arbitrary temp workspace, so create the equivalent
        symlink on each start.
        """
        workspace = Path(cwd)
        source = skills_source or Path(__file__).resolve().parents[1] / "claude_skills"
        if not source.exists():
            logger.warning("[LocalClaudeBridge] Skills source does not exist: %s", source)
            return

        claude_dir = workspace / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        target = claude_dir / "skills"

        try:
            if target.is_symlink() or target.exists():
                if target.resolve() == source.resolve():
                    return
                logger.warning(
                    "[LocalClaudeBridge] Not replacing existing skills path: %s",
                    target,
                )
                return
            target.symlink_to(source, target_is_directory=True)
        except Exception as exc:
            logger.warning(
                "[LocalClaudeBridge] Failed to expose local Claude skills: %s",
                exc,
            )

    async def read_stream(self):
        """Read JSON lines from the subprocess stdout."""
        if self._process is None or self._process.stdout is None:
            return

        async for line in self._process.stdout:
            line_str = line.decode("utf-8").strip()
            if not line_str:
                continue
            try:
                yield json.loads(line_str)
            except json.JSONDecodeError:
                logger.warning("[LocalClaudeBridge] Skipping non-JSON line: %s", line_str[:100])

        # Read stderr for diagnostics
        if self._process.stderr:
            stderr_data = await self._process.stderr.read()
            if stderr_data:
                stderr_text = stderr_data.decode("utf-8", errors="replace")
                for stderr_line in stderr_text.strip().split("\n"):
                    if stderr_line.strip():
                        logger.info("[LocalClaudeBridge stderr] %s", stderr_line)

    async def make_workspace_dir(self, path: str) -> None:
        target = _resolve_local_workspace_target(self._cwd, path)
        target.mkdir(parents=True, exist_ok=True)

    async def write_workspace_file_bytes(self, path: str, data: bytes) -> None:
        target = _resolve_local_workspace_target(self._cwd, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    async def cleanup(self) -> None:
        if self._process is not None:
            try:
                if self._process.returncode is None:
                    self._process.kill()
                    await self._process.wait()
            except Exception:
                pass
            self._process = None
