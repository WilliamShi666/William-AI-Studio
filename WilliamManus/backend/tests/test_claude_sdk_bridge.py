"""
Tests for SandboxClaudeBridge and FakeClaudeBridge.
"""

import json
from unittest.mock import AsyncMock, patch
from pathlib import Path

import pytest

from agentscope_integration.claude_sdk_bridge import (
    FakeClaudeBridge,
    LocalClaudeBridge,
    SandboxClaudeBridge,
)


class TestFakeClaudeBridge:
    """Tests for FakeClaudeBridge — the in-process test bridge."""

    @pytest.mark.asyncio
    async def test_start_returns_ok(self):
        bridge = FakeClaudeBridge()
        result = await bridge.start(prompt="hi", thread_id="t1")
        assert result == "ok"
        assert bridge._started is True

    @pytest.mark.asyncio
    async def test_read_stream_yields_messages(self):
        messages = [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}},
            {"type": "result", "subtype": "success", "result": "Done."},
        ]
        bridge = FakeClaudeBridge(messages)

        output = []
        async for msg in bridge.read_stream():
            output.append(msg)

        assert len(output) == 2
        assert output[0]["type"] == "stream_event"
        assert output[1]["type"] == "result"

    @pytest.mark.asyncio
    async def test_read_stream_empty_when_no_messages(self):
        bridge = FakeClaudeBridge()
        output = []
        async for msg in bridge.read_stream():
            output.append(msg)
        assert output == []

    @pytest.mark.asyncio
    async def test_cleanup_sets_flag(self):
        bridge = FakeClaudeBridge()
        await bridge.cleanup()
        assert bridge._cleaned_up is True


class TestSandboxClaudeBridge:
    """Tests for SandboxClaudeBridge construction."""

    def test_requires_api_key(self):
        """Constructor stores params; start() validates API key at runtime."""
        bridge = SandboxClaudeBridge(deepseek_api_key="")
        assert bridge._deepseek_api_key == ""

    def test_stores_constructor_params(self):
        bridge = SandboxClaudeBridge(
            deepseek_api_key="sk-test-key",
            template_name="claude-agent-v1",
            model="deepseek-v4-pro[1m]",
            sandbox_timeout=1800,
        )
        assert bridge._deepseek_api_key == "sk-test-key"
        assert bridge._template_name == "claude-agent-v1"
        assert bridge._model == "deepseek-v4-pro[1m]"
        assert bridge._sandbox_timeout == 1800


    @pytest.mark.asyncio
    async def test_start_prepares_workspace_as_root_before_launching_service(self):
        """Claude runs as user, so /workspace must be made writable before SDK service starts."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        class FakeFiles:
            def __init__(self):
                self.writes = []

            def write(self, path, data, **kwargs):
                self.writes.append((path, data, kwargs))

        class FakeCommands:
            def __init__(self):
                self.calls = []

            def run(self, command, **kwargs):
                self.calls.append((command, kwargs))
                return object()

        class FakeSandbox:
            def __init__(self):
                self.files = FakeFiles()
                self.commands = FakeCommands()

        fake_sandbox = FakeSandbox()

        with patch.object(bridge, "_create_sandbox", new=AsyncMock(return_value=fake_sandbox)):
            await bridge.start(prompt="hi", thread_id="thread-1")

        commands = [command for command, _kwargs in fake_sandbox.commands.calls]
        prepare_command, prepare_kwargs = fake_sandbox.commands.calls[0]
        service_command, service_kwargs = fake_sandbox.commands.calls[-1]

        assert prepare_command in commands[:1]
        assert "mkdir -p /workspace" in prepare_command
        assert "chown -R user:user /workspace" in prepare_command
        assert "chmod 777 /workspace" in prepare_command
        assert prepare_kwargs["user"] == "root"
        assert "python3 /opt/claude_agent_service.py" in service_command
        assert service_kwargs["background"] is True
        assert fake_sandbox.files.writes[0][0] == "/tmp/claude_request.json"

    @pytest.mark.asyncio
    async def test_start_redirects_sandbox_service_to_jsonl_files(self):
        """Sandbox service stdout/stderr should be persisted for polling read_stream."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        class FakeFiles:
            def __init__(self):
                self.writes = []

            def write(self, path, data, **kwargs):
                self.writes.append((path, data, kwargs))

        class FakeCommands:
            def __init__(self):
                self.calls = []

            def run(self, command, **kwargs):
                self.calls.append((command, kwargs))
                return object()

        class FakeSandbox:
            def __init__(self):
                self.files = FakeFiles()
                self.commands = FakeCommands()

        fake_sandbox = FakeSandbox()

        with patch.object(bridge, "_create_sandbox", new=AsyncMock(return_value=fake_sandbox)):
            await bridge.start(prompt="hi", thread_id="thread-1")

        command, kwargs = fake_sandbox.commands.calls[-1]
        request_payload = json.loads(fake_sandbox.files.writes[0][1])
        assert "python3 /opt/claude_agent_service.py" in command
        assert "PATH=/opt/npm-global/bin:" in command
        assert "< /tmp/claude_request.json" in command
        assert "> /tmp/claude_output.jsonl" in command
        assert "2> /tmp/claude_error.log" in command
        assert "stdin" not in kwargs
        assert kwargs["background"] is True
        assert fake_sandbox.files.writes[0][0] == "/tmp/claude_request.json"
        assert request_payload["prompt"] == "hi"
        assert request_payload["env"]["PATH"].startswith("/opt/npm-global/bin:")


    @pytest.mark.asyncio
    async def test_workspace_file_helpers_use_sandbox_files_api(self):
        """Sandbox workspace persistence should list and read through PPIO/E2B files API."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        class FileType:
            def __init__(self, value):
                self.value = value

        class Entry:
            def __init__(self, path, type_, size):
                self.path = path
                self.type = type_
                self.size = size

        class FakeFiles:
            def __init__(self):
                self.list_calls = []
                self.read_calls = []

            def list(self, path, depth=1, **kwargs):
                self.list_calls.append((path, depth, kwargs))
                return [
                    Entry("/workspace/a.md", FileType("file"), 5),
                    Entry("/workspace/nested", FileType("dir"), 0),
                ]

            def read(self, path, format="text", **kwargs):
                self.read_calls.append((path, format, kwargs))
                return bytearray(b"hello")

        class FakeSandbox:
            def __init__(self):
                self.files = FakeFiles()

        sandbox = FakeSandbox()
        bridge._sandbox = sandbox

        entries = await bridge.list_workspace_files("/workspace")
        content = await bridge.read_workspace_file_bytes("/workspace/a.md")

        assert entries == [{"path": "/workspace/a.md", "type": "file", "size": 5}]
        assert sandbox.files.list_calls == [("/workspace", 20, {})]
        assert sandbox.files.read_calls == [("/workspace/a.md", "bytes", {})]
        assert content == b"hello"

    @pytest.mark.asyncio
    async def test_start_supports_async_ppio_sandbox_methods(self):
        """PPIO AsyncSandbox exposes awaitable files.write and commands.run methods."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        class FakeAsyncFiles:
            def __init__(self):
                self.writes = []

            async def write(self, path, data, **kwargs):
                self.writes.append((path, data, kwargs))

        class FakeAsyncCommands:
            def __init__(self):
                self.calls = []

            async def run(self, command, **kwargs):
                self.calls.append((command, kwargs))
                return object()

        class FakeAsyncSandbox:
            def __init__(self):
                self.files = FakeAsyncFiles()
                self.commands = FakeAsyncCommands()

        fake_sandbox = FakeAsyncSandbox()

        with patch.object(bridge, "_create_sandbox", new=AsyncMock(return_value=fake_sandbox)):
            await bridge.start(prompt="hi", thread_id="thread-1")

        assert fake_sandbox.files.writes
        assert fake_sandbox.commands.calls

    @pytest.mark.asyncio
    async def test_read_sandbox_text_awaits_async_ppio_commands(self):
        """Polling should support PPIO AsyncSandbox.commands.run coroutine results."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        class Result:
            stdout = "hello"

        class FakeAsyncCommands:
            async def run(self, command, **kwargs):
                return Result()

        class FakeAsyncSandbox:
            commands = FakeAsyncCommands()

        bridge._sandbox = FakeAsyncSandbox()

        assert await bridge._read_sandbox_text("/tmp/out") == "hello"

    @pytest.mark.asyncio
    async def test_read_sandbox_text_prefers_files_read(self):
        """Polling should read JSONL files through the sandbox filesystem API when available."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        class FakeFiles:
            def __init__(self):
                self.paths = []

            def read(self, path, **kwargs):
                self.paths.append((path, kwargs))
                return "from-files"

        class FakeCommands:
            def run(self, command, **kwargs):
                raise AssertionError("commands.run should not be used when files.read is available")

        class FakeSandbox:
            def __init__(self):
                self.files = FakeFiles()
                self.commands = FakeCommands()

        bridge._sandbox = FakeSandbox()

        assert await bridge._read_sandbox_text("/tmp/out") == "from-files"

    @pytest.mark.asyncio
    async def test_read_stream_polls_incremental_jsonl_until_result(self):
        """Sandbox read_stream should parse new JSONL lines incrementally and stop on result."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        snapshots = [
            '',
            '{"type":"stream_event","event":{"type":"message_start"}}\n',
            '{"type":"stream_event","event":{"type":"message_start"}}\n'
            '{"type":"result","subtype":"success","result":"Done"}\n',
        ]
        state = {"index": 0}

        async def fake_read(path):
            if "claude_done" in path or "claude_error" in path or "claude_exit_code" in path:
                return ""
            value = snapshots[min(state["index"], len(snapshots) - 1)]
            state["index"] += 1
            return value

        bridge._sandbox = object()
        bridge._process = object()

        messages = []
        with patch.object(bridge, "_read_sandbox_text", new=fake_read):
            async for item in bridge.read_stream(poll_interval_seconds=0.001, timeout_seconds=1):
                messages.append(item)

        assert [item["type"] for item in messages] == ["stream_event", "result"]
        assert messages[-1]["result"] == "Done"

    @pytest.mark.asyncio
    async def test_read_stream_reports_stderr_on_malformed_json_timeout(self):
        """Malformed sandbox output should surface stderr context instead of hanging silently."""
        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")

        async def fake_read(path):
            if "claude_error" in path:
                return "sdk exploded"
            if "claude_done" in path or "claude_exit_code" in path:
                return ""
            return '{"type":"stream_event"'

        bridge._sandbox = object()
        bridge._process = object()

        messages = []
        with patch.object(bridge, "_read_sandbox_text", new=fake_read):
            async for item in bridge.read_stream(poll_interval_seconds=0.001, timeout_seconds=0.01):
                messages.append(item)

        assert messages[-1]["type"] == "result"
        assert messages[-1]["is_error"] is True
        assert "sdk exploded" in messages[-1]["result"]

    @pytest.mark.asyncio
    async def test_start_without_api_key_raises(self):
        bridge = SandboxClaudeBridge(deepseek_api_key="")
        with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY"):
            await bridge.start(prompt="test")


class TestLocalClaudeBridge:
    """Tests for local Claude SDK bridge skill discovery setup."""

    def test_ensure_local_skills_link_creates_project_claude_skills_symlink(self, tmp_path):
        skills_source = tmp_path / "backend-skills"
        skills_source.mkdir()
        (skills_source / "summarize").mkdir()
        (skills_source / "summarize" / "SKILL.md").write_text(
            "---\nname: summarize\n---\nSummarize things.\n"
        )

        workspace = tmp_path / "workspace"
        workspace.mkdir()

        LocalClaudeBridge._ensure_local_skills_link(
            cwd=str(workspace),
            skills_source=skills_source,
        )

        linked = workspace / ".claude" / "skills"
        assert linked.is_symlink()
        assert Path(linked.resolve()) == skills_source.resolve()

    def test_local_workspace_system_prompt_maps_workspace_to_cwd(self):
        bridge = LocalClaudeBridge(deepseek_api_key="sk-test", cwd="/tmp/run-workspace")

        prompt = bridge._build_local_system_prompt("base prompt")

        assert "base prompt" in prompt
        assert "/workspace" in prompt
        assert "current working directory" in prompt
        assert "relative path" in prompt

    @pytest.mark.asyncio
    async def test_start_mounts_run_workspace_at_absolute_workspace_for_local_mode(
        self,
        tmp_path,
        monkeypatch,
    ):
        """Absolute /workspace writes must land in the per-run local workspace.

        A prompt-only mapping is not an isolation boundary: Claude Code can use
        absolute /workspace paths and bypass cwd. Local mode therefore launches
        the service in a mount namespace where /workspace is bind-mounted to
        the run directory, while the host-side bridge still persists the run
        directory afterward.
        """
        workspace = tmp_path / "local-root" / "runs" / "run-workspace"
        bridge = LocalClaudeBridge(deepseek_api_key="sk-test", cwd=str(workspace))
        monkeypatch.setenv("CLAUDE_LOCAL_BIND_WORKSPACE", "1")

        class FakeStdin:
            def __init__(self):
                self.payload = b""
                self.closed = False

            def write(self, data):
                self.payload += data

            async def drain(self):
                return None

            def close(self):
                self.closed = True

        class FakeProcess:
            def __init__(self):
                self.stdin = FakeStdin()
                self.stdout = None
                self.stderr = None
                self.returncode = 0

        fake_process = FakeProcess()

        with patch(
            "agentscope_integration.claude_sdk_bridge.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=fake_process),
        ) as create_subprocess:
            await bridge.start(prompt="Create /workspace/out.md", thread_id="thread-1")

        command = create_subprocess.await_args.args
        kwargs = create_subprocess.await_args.kwargs
        assert command[:5] == ("unshare", "--user", "--map-root-user", "--mount", "bash")
        assert 'mount --bind "$CLAUDE_LOCAL_WORKSPACE_DIR" /workspace' in command[6]
        assert 'mount -t tmpfs -o size=64m,mode=700 tmpfs "$CLAUDE_LOCAL_WORKSPACE_ROOT"' in command[6]
        assert 'mount -t tmpfs -o size=64m,mode=700 tmpfs /tmp/claude-test-workspace' in command[6]
        assert "$CLAUDE_LOCAL_WORKSPACE_DIR" in command[6]
        assert kwargs["env"]["CLAUDE_LOCAL_WORKSPACE_DIR"] == str(workspace)
        assert kwargs["env"]["CLAUDE_LOCAL_WORKSPACE_ROOT"] == str(tmp_path / "local-root")
        request = json.loads(fake_process.stdin.payload.decode("utf-8"))
        assert request["cwd"] == "/workspace"
        assert request["permission_mode"] == "acceptEdits"
        assert bridge.workspace_dir == str(workspace)

    def test_namespace_workspace_root_never_resolves_to_filesystem_root(self):
        """Custom local cwd values must not make the namespace hide /."""
        bridge = LocalClaudeBridge(deepseek_api_key="sk-test", cwd="/tmp/custom-run")

        env = bridge._service_process_env()

        assert env["CLAUDE_LOCAL_WORKSPACE_ROOT"] == "/tmp/custom-run"

    def test_unshare_missing_fails_closed_by_default(self, monkeypatch):
        """Production local mode must not silently run without /workspace isolation."""
        bridge = LocalClaudeBridge(deepseek_api_key="sk-test", cwd="/tmp/custom-run")
        monkeypatch.setenv("CLAUDE_LOCAL_BIND_WORKSPACE", "1")
        monkeypatch.delenv("CLAUDE_LOCAL_ALLOW_UNISOLATED_FALLBACK", raising=False)
        monkeypatch.setattr("agentscope_integration.claude_sdk_bridge.shutil.which", lambda _name: None)

        with pytest.raises(RuntimeError, match="refusing to run local Claude SDK"):
            bridge._service_command()

    def test_unshare_missing_requires_explicit_unsafe_fallback(self, monkeypatch):
        """Developer-only fallback is opt-in and still avoids bypassPermissions."""
        bridge = LocalClaudeBridge(deepseek_api_key="sk-test", cwd="/tmp/custom-run")
        monkeypatch.setenv("CLAUDE_LOCAL_BIND_WORKSPACE", "1")
        monkeypatch.setenv("CLAUDE_LOCAL_ALLOW_UNISOLATED_FALLBACK", "1")
        monkeypatch.setattr("agentscope_integration.claude_sdk_bridge.shutil.which", lambda _name: None)

        assert bridge._service_command() == ("python3", bridge._service_path)
        assert bridge._service_permission_mode() == "acceptEdits"
