"""
Tests for ClaudeSDKRunner — the WilliamManus runner that delegates to
Claude Agent SDK running inside a PPIO sandbox.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentscope_integration.claude_sdk_runner import (
    ClaudeSDKRunner,
    _persist_run_messages,
)


# ── helpers ──

def _make_stream_event(text):
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    }


def _make_assistant_message(text, tool_calls=None):
    content = [{"type": "text", "text": text}]
    if tool_calls:
        content.extend(tool_calls)
    return {"type": "assistant", "message": {"id": "msg_1", "content": content}}


def _make_tool_use_block(id_, name, input_):
    return {"type": "tool_use", "id": id_, "name": name, "input": input_}


def _make_tool_result(tool_use_id, content):
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}]},
    }


def _make_result():
    return {"type": "result", "subtype": "success", "result": "Done."}


class FakeSandboxBridge:
    """Simulates the sandbox-resident Claude Agent Service."""

    def __init__(self, messages):
        self._messages = messages
        self._started = False
        self._cleaned_up = False
        self.start_kwargs = {}
        self.prompt = None
        self.events = []

    async def start(self, prompt, system_prompt=None, thread_id=None):
        self._started = True
        self.prompt = prompt
        self.start_kwargs = {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "thread_id": thread_id,
        }
        return "ok"

    async def read_stream(self):
        for msg in self._messages:
            yield msg

    async def cleanup(self):
        self.events.append("cleanup")
        self._cleaned_up = True


class FakeMessagesTable:
    def __init__(self):
        self.inserts = []

    async def insert(self, payload):
        self.inserts.append(payload)


class FakeDbClient:
    def __init__(self, select_rows=None, event_rows=None):
        self.messages_table = FakeMessagesTable()
        self.select_rows = select_rows or []
        self.event_rows = event_rows or []

    def table(self, table_name):
        assert table_name in {"messages", "events"}
        if table_name == "events":
            return FakeMessagesQuery(self.event_rows)
        if self.select_rows:
            return FakeMessagesTableAndQuery(self.messages_table, self.select_rows)
        return self.messages_table

    def schema(self, _schema_name):
        return self


class FakeQueryResult:
    def __init__(self, data):
        self.data = data


class FakeMessagesQuery:
    def __init__(self, rows):
        self.rows = rows
        self.filters = {}
        self.limit_value = None
        self.desc = False

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def order(self, _key, desc=False):
        self.desc = desc
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    async def execute(self):
        rows = [
            row
            for row in self.rows
            if all(row.get(key) == value for key, value in self.filters.items())
        ]
        if self.desc:
            rows = list(reversed(rows))
        if self.limit_value:
            rows = rows[: self.limit_value]
        return FakeQueryResult(rows)


class FakeMessagesTableAndQuery(FakeMessagesQuery):
    def __init__(self, table, rows):
        super().__init__(rows)
        self._table = table

    async def insert(self, payload):
        return await self._table.insert(payload)


class TestClaudeSDKRunner:
    """Tests for ClaudeSDKRunner.run() SSE streaming."""

    @pytest.fixture
    def mock_db(self):
        return FakeDbClient()

    @pytest.fixture
    def runner(self, mock_db):
        return ClaudeSDKRunner(
            thread_id="thread-1",
            project_id="project-1",
            model_key="claude-sdk",
            db_client=mock_db,
        )

    # ── Constructor ──

    def test_constructor_stores_params(self, mock_db):
        runner = ClaudeSDKRunner(
            thread_id="t1",
            project_id="p1",
            model_key="deepseek-v4-pro",
            db_client=mock_db,
            system_prompt="orchestrator prompt",
        )
        assert runner.thread_id == "t1"
        assert runner.project_id == "p1"
        assert runner.model_key == "deepseek-v4-pro"
        assert runner.system_prompt == "orchestrator prompt"

    # ── run(): text-only flow ──

    @pytest.mark.asyncio
    async def test_run_streams_text_chunks(self, runner):
        """Given a stream of text_delta events, run() yields SSE chunk dicts."""
        messages = [
            _make_stream_event("Hello"),
            _make_stream_event(" world"),
            _make_assistant_message("Hello world"),
            _make_result(),
        ]
        bridge = FakeSandboxBridge(messages)

        with patch.object(runner, "_get_bridge", return_value=bridge):
            chunks = []
            async for chunk in runner.run(user_message="Say hi", thread_run_id="run-1"):
                chunks.append(chunk)

        # Should have at least: 2 text chunks + 1 complete + 1 terminal
        assert len(chunks) >= 4

        # First two should be "chunk" stream_status
        assert json.loads(chunks[0]["metadata"])["stream_status"] == "chunk"
        assert json.loads(chunks[1]["metadata"])["stream_status"] == "chunk"

        # The complete message has a message_id
        complete_idx = 2
        assert chunks[complete_idx]["message_id"] is not None
        assert json.loads(chunks[complete_idx]["metadata"])["stream_status"] == "complete"

        # Last should be terminal status
        assert chunks[-1]["type"] == "status"
        assert chunks[-1]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_passes_orchestrator_system_prompt_to_bridge(self, mock_db):
        """Claude SDK runs must receive the AgentScope orchestrator system prompt."""
        runner = ClaudeSDKRunner(
            thread_id="thread-1",
            project_id="project-1",
            model_key="claude-sdk",
            db_client=mock_db,
            system_prompt="AGENTSCOPE_ORCHESTRATOR_PROMPT_WITH_SKILLS",
        )
        bridge = FakeSandboxBridge([_make_result()])

        with patch.object(runner, "_get_bridge", return_value=bridge):
            chunks = []
            async for chunk in runner.run(user_message="Say hi", thread_run_id="run-1"):
                chunks.append(chunk)

        assert bridge.start_kwargs["prompt"] == "Say hi"
        assert bridge.start_kwargs["thread_id"] == "thread-1"
        assert (
            bridge.start_kwargs["system_prompt"]
            == "AGENTSCOPE_ORCHESTRATOR_PROMPT_WITH_SKILLS"
        )
        assert chunks[-1]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_run_prepends_bounded_thread_history_to_prompt(self):
        """Claude SDK backend should replay recent user/assistant text context."""
        history_db = FakeDbClient(
            select_rows=[
                {
                    "thread_id": "thread-1",
                    "type": "user",
                    "role": "user",
                    "content": json.dumps(
                        {"role": "user", "content": "My project codename is ORBITAL-7"}
                    ),
                    "metadata": json.dumps({}),
                    "created_at": "2026-05-19T00:00:00Z",
                },
                {
                    "thread_id": "thread-1",
                    "type": "assistant",
                    "role": "assistant",
                    "content": json.dumps(
                        {"role": "assistant", "content": "I will remember ORBITAL-7."}
                    ),
                    "metadata": json.dumps({"stream_status": "complete"}),
                    "created_at": "2026-05-19T00:01:00Z",
                },
                {
                    "thread_id": "other-thread",
                    "type": "user",
                    "role": "user",
                    "content": json.dumps({"role": "user", "content": "do not include"}),
                    "metadata": json.dumps({}),
                    "created_at": "2026-05-19T00:02:00Z",
                },
            ]
        )
        runner = ClaudeSDKRunner(
            thread_id="thread-1",
            project_id="project-1",
            model_key="claude-sdk",
            db_client=history_db,
        )
        bridge = FakeSandboxBridge([_make_result()])

        with patch.object(runner, "_get_bridge", return_value=bridge):
            async for _chunk in runner.run(
                user_message="What codename did I give you?",
                thread_run_id="run-1",
            ):
                pass

        assert "<conversation_history>" in bridge.prompt
        assert "user: My project codename is ORBITAL-7" in bridge.prompt
        assert "assistant: I will remember ORBITAL-7." in bridge.prompt
        assert "What codename did I give you?" in bridge.prompt
        assert "do not include" not in bridge.prompt

    @pytest.mark.asyncio
    async def test_run_omits_duplicate_latest_user_history(self):
        """The current user message should appear once: outside the history block."""
        history_db = FakeDbClient(
            select_rows=[
                {
                    "thread_id": "thread-1",
                    "type": "user",
                    "role": "user",
                    "content": json.dumps(
                        {"role": "user", "content": "Repeat question exactly"}
                    ),
                    "metadata": json.dumps({}),
                    "created_at": "2026-05-19T00:00:00Z",
                }
            ]
        )
        runner = ClaudeSDKRunner(
            thread_id="thread-1",
            project_id="project-1",
            model_key="claude-sdk",
            db_client=history_db,
        )
        bridge = FakeSandboxBridge([_make_result()])

        with patch.object(runner, "_get_bridge", return_value=bridge):
            async for _chunk in runner.run(
                user_message="Repeat question exactly",
                thread_run_id="run-1",
            ):
                pass

        assert "<conversation_history>" not in bridge.prompt
        assert bridge.prompt == "Repeat question exactly"

    @pytest.mark.asyncio
    async def test_run_loads_prior_user_turns_from_events_table(self):
        """Frontend-created user turns live in events; Claude SDK history must include them."""
        history_db = FakeDbClient(
            select_rows=[
                {
                    "thread_id": "thread-1",
                    "type": "assistant",
                    "role": "assistant",
                    "content": json.dumps(
                        {"role": "assistant", "content": "Noted: your favorite color is blue."}
                    ),
                    "metadata": json.dumps({"stream_status": "complete"}),
                    "created_at": "2026-05-19T00:01:00Z",
                },
            ],
            event_rows=[
                {
                    "session_id": "thread-1",
                    "author": "user",
                    "content": json.dumps({"content": "My favorite color is blue."}),
                    "timestamp": "2026-05-19T00:00:00Z",
                },
                {
                    "session_id": "thread-1",
                    "author": "user",
                    "content": json.dumps({"content": "What color did I tell you?"}),
                    "timestamp": "2026-05-19T00:02:00Z",
                },
            ],
        )
        runner = ClaudeSDKRunner(
            thread_id="thread-1",
            project_id="project-1",
            model_key="claude-sdk",
            db_client=history_db,
        )
        bridge = FakeSandboxBridge([_make_result()])

        with patch.object(runner, "_get_bridge", return_value=bridge):
            async for _chunk in runner.run(
                user_message="What color did I tell you?",
                thread_run_id="run-1",
            ):
                pass

        assert "user: My favorite color is blue." in bridge.prompt
        assert "assistant: Noted: your favorite color is blue." in bridge.prompt
        assert bridge.prompt.count("What color did I tell you?") == 1

    # ── run(): tool call + result flow ──

    @pytest.mark.asyncio
    async def test_run_streams_tool_calls_and_results(self, runner):
        """Given tool_use + tool_result, run() yields tool_call and tool result chunks."""
        messages = [
            _make_assistant_message(
                "Let me check.",
                tool_calls=[_make_tool_use_block("call_1", "Read", {"file_path": "/f"})],
            ),
            _make_tool_result("call_1", "contents of file"),
            _make_assistant_message("File says: contents of file"),
            _make_result(),
        ]
        bridge = FakeSandboxBridge(messages)

        with patch.object(runner, "_get_bridge", return_value=bridge):
            chunks = []
            async for chunk in runner.run(user_message="Read /f", thread_run_id="run-1"):
                chunks.append(chunk)

        # Should contain at least: complete (with tool_calls) + tool_result + complete (text) + terminal
        assert len(chunks) >= 4

        # tool result chunk
        tool_chunks = [c for c in chunks if c["type"] == "tool"]
        assert len(tool_chunks) >= 1
        tool_content = json.loads(tool_chunks[0]["content"])
        assert tool_content["tool_name"] == "Read"
        assert tool_content["tool_call_id"] == "call_1"
        assert "contents of file" in tool_content["result"]

    # ── run(): error handling ──

    @pytest.mark.asyncio
    async def test_run_yields_error_on_exception(self, runner):
        """When the bridge raises, run() yields an error SSE message."""
        bridge = FakeSandboxBridge([])
        bridge.start = AsyncMock(side_effect=RuntimeError("Sandbox unreachable"))

        with patch.object(runner, "_get_bridge", return_value=bridge):
            chunks = []
            async for chunk in runner.run(user_message="hi", thread_run_id="run-1"):
                chunks.append(chunk)

        error_chunks = [c for c in chunks if c["type"] == "status" and c.get("status") == "failed"]
        assert len(error_chunks) >= 1

    # ── close() cleanup ──

    @pytest.mark.asyncio
    async def test_close_cleans_up(self, runner):
        bridge = FakeSandboxBridge([])
        with patch.object(runner, "_get_bridge", return_value=bridge):
            runner._bridge = bridge
            await runner.close()

        assert bridge._cleaned_up is True

    @pytest.mark.asyncio
    async def test_close_noop_when_no_bridge(self, runner):
        """close() should not raise when no bridge was created."""
        await runner.close()  # no exception

    # ── file persistence ──


    @pytest.mark.asyncio
    async def test_run_persists_messages_and_workspace_before_terminal_status_is_yielded(self, runner):
        """Terminal completed status must not outrun artifact persistence and sandbox inspection."""
        messages = [
            _make_stream_event("Created file."),
            _make_result(),
        ]
        bridge = FakeSandboxBridge(messages)
        bridge.workspace_dir = "/workspace"
        bridge.list_workspace_files = AsyncMock(
            side_effect=lambda root="/workspace": bridge.events.append("list_workspace_files")
            or [{"path": "/workspace/a.md", "size": 2}]
        )
        bridge.read_workspace_file_bytes = AsyncMock(
            side_effect=lambda path: bridge.events.append(f"read:{path}") or b"ok"
        )

        persisted = []

        async def fake_persist_artifact(**kwargs):
            bridge.events.append("persist_artifact")
            persisted.append(kwargs)

        with patch.object(runner, "_get_bridge", return_value=bridge), patch(
            "services.workspace_artifacts.workspace_artifacts.persist_artifact",
            side_effect=fake_persist_artifact,
        ):
            chunks = []
            async for chunk in runner.run(user_message="write file", thread_run_id="run-before-status"):
                chunks.append(chunk)
                if chunk.get("type") == "status":
                    assert bridge.events == [
                        "list_workspace_files",
                        "read:/workspace/a.md",
                        "persist_artifact",
                    ]

        assert chunks[-1]["status"] == "completed"
        assert persisted[0]["path"] == "/workspace/a.md"


    @pytest.mark.asyncio
    async def test_run_still_yields_terminal_status_when_artifact_persistence_fails(self, runner):
        """Artifact persistence errors should be logged, not hide the terminal status."""
        bridge = FakeSandboxBridge([_make_result()])
        bridge.workspace_dir = "/workspace"
        bridge.list_workspace_files = AsyncMock(
            return_value=[{"path": "/workspace/a.md", "size": 2}]
        )
        bridge.read_workspace_file_bytes = AsyncMock(return_value=b"ok")

        with patch.object(runner, "_get_bridge", return_value=bridge), patch(
            "services.workspace_artifacts.workspace_artifacts.persist_artifact",
            side_effect=RuntimeError("persist failed"),
        ):
            chunks = []
            async for chunk in runner.run(user_message="write file", thread_run_id="run-persist-fails"):
                chunks.append(chunk)

        assert chunks[-1]["type"] == "status"
        assert chunks[-1]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_persist_workspace_files_uses_sandbox_bridge_file_api(self):
        """Sandbox bridges expose /workspace through bridge APIs, not host os.walk."""
        from agentscope_integration.claude_sdk_runner import _persist_workspace_files

        class SandboxFileBridge(FakeSandboxBridge):
            workspace_dir = "/workspace"
            sandbox_id = "sandbox-real-1"

            async def list_workspace_files(self, root="/workspace"):
                assert root == "/workspace"
                return [
                    {"path": "/workspace/result.md", "size": 5},
                    {"path": "/workspace/.claude/settings.json", "size": 2},
                ]

            async def read_workspace_file_bytes(self, path):
                assert path == "/workspace/result.md"
                return b"hello"

        bridge = SandboxFileBridge([])
        mock_artifacts = AsyncMock()
        mock_artifacts.persist_artifact = AsyncMock()

        with patch("services.workspace_artifacts.workspace_artifacts", mock_artifacts):
            await _persist_workspace_files(
                bridge=bridge,
                agent_run_id="run-sandbox",
                project_id="proj-sandbox",
                thread_id="thread-sandbox",
            )

        mock_artifacts.persist_artifact.assert_called_once()
        call_kwargs = mock_artifacts.persist_artifact.call_args.kwargs
        assert call_kwargs["path"] == "/workspace/result.md"
        assert call_kwargs["content"] == b"hello"
        assert call_kwargs["source"] == "claude_sdk_sandbox"
        assert call_kwargs["sandbox_id"] == "sandbox-real-1"
        assert call_kwargs["metadata"]["workspace_scope"] == "agent_run"
        assert call_kwargs["metadata"]["bridge_type"] == "SandboxFileBridge"

    @pytest.mark.asyncio
    async def test_persist_workspace_files_skips_when_no_dir(self):
        """_persist_workspace_files is a no-op when bridge has no workspace_dir."""
        from agentscope_integration.claude_sdk_runner import _persist_workspace_files

        bridge = FakeSandboxBridge([])
        await _persist_workspace_files(
            bridge=bridge,
            agent_run_id="run-1",
            project_id="proj-1",
            thread_id="thread-1",
        )
        # Should not raise; bridge has no workspace_dir attribute.

    @pytest.mark.asyncio
    async def test_persist_workspace_files_skips_when_dir_missing(self):
        """_persist_workspace_files is a no-op when workspace_dir doesn't exist."""
        from agentscope_integration.claude_sdk_runner import _persist_workspace_files

        bridge = FakeSandboxBridge([])
        bridge.workspace_dir = "/nonexistent/path"
        await _persist_workspace_files(
            bridge=bridge,
            agent_run_id="run-1",
            project_id="proj-1",
            thread_id="thread-1",
        )
        # Should not raise.

    @pytest.mark.asyncio
    async def test_persist_workspace_files_calls_persist_artifact(self, tmp_path):
        """_persist_workspace_files calls workspace_artifacts.persist_artifact."""
        from unittest.mock import AsyncMock, patch
        from agentscope_integration.claude_sdk_runner import _persist_workspace_files

        workspace = str(tmp_path)
        (tmp_path / "hello.py").write_text("print('hello')")

        bridge = FakeSandboxBridge([])
        bridge.workspace_dir = workspace

        mock_artifacts = AsyncMock()
        mock_artifacts.persist_artifact = AsyncMock()
        mock_is_visible = patch(
            "services.workspace_artifacts.is_user_visible_workspace_artifact_path",
            return_value=True,
        )

        with patch(
            "services.workspace_artifacts.workspace_artifacts",
            mock_artifacts,
        ), mock_is_visible:
            await _persist_workspace_files(
                bridge=bridge,
                agent_run_id="run-files",
                project_id="proj-files",
                thread_id="thread-files",
            )

        mock_artifacts.persist_artifact.assert_called_once()
        call_kwargs = mock_artifacts.persist_artifact.call_args.kwargs
        assert call_kwargs["agent_run_id"] == "run-files"
        assert call_kwargs["project_id"] == "proj-files"
        assert call_kwargs["source"] == "claude_sdk_local"
        assert "claude-local:run-files" in call_kwargs["sandbox_id"]


    @pytest.mark.asyncio
    async def test_persist_workspace_files_skips_internal_claude_directory_and_symlinks(self, tmp_path):
        """Local persistence must not expose internal project config or symlink targets."""
        from unittest.mock import AsyncMock, patch
        from agentscope_integration.claude_sdk_runner import _persist_workspace_files

        (tmp_path / "visible.txt").write_text("visible")
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.json").write_text("{}")
        (tmp_path / "leak.txt").symlink_to("/etc/passwd")

        bridge = FakeSandboxBridge([])
        bridge.workspace_dir = str(tmp_path)

        mock_artifacts = AsyncMock()
        mock_artifacts.persist_artifact = AsyncMock()

        with patch("services.workspace_artifacts.workspace_artifacts", mock_artifacts):
            await _persist_workspace_files(
                bridge=bridge,
                agent_run_id="run-secure",
                project_id="proj-secure",
                thread_id="thread-secure",
            )

        persisted_paths = [
            call.kwargs["path"]
            for call in mock_artifacts.persist_artifact.call_args_list
        ]
        assert persisted_paths == ["/workspace/visible.txt"]
        call_kwargs = mock_artifacts.persist_artifact.call_args.kwargs
        assert call_kwargs["metadata"]["workspace_scope"] == "agent_run"
        assert call_kwargs["agent_run_id"] == "run-secure"

    @pytest.mark.asyncio
    async def test_persist_run_messages_preserves_chunk_text_when_final_assistant_is_empty(self):
        """Durable history should retain live streamed text even if the SDK final message is empty."""
        db_client = FakeDbClient()
        messages = [
            {
                "type": "assistant",
                "message_id": None,
                "is_llm_message": True,
                "content": json.dumps({"role": "assistant", "content": "Hello"}),
                "metadata": json.dumps({"stream_status": "chunk", "thread_run_id": "run-1"}),
            },
            {
                "type": "assistant",
                "message_id": None,
                "is_llm_message": True,
                "content": json.dumps({"role": "assistant", "content": " world"}),
                "metadata": json.dumps({"stream_status": "chunk", "thread_run_id": "run-1"}),
            },
            {
                "type": "assistant",
                "message_id": "empty-final",
                "is_llm_message": True,
                "content": json.dumps({"role": "assistant", "content": ""}),
                "metadata": json.dumps({"stream_status": "complete", "thread_run_id": "run-1"}),
            },
            {
                "type": "status",
                "status": "completed",
                "message": "Done.",
            },
        ]

        await _persist_run_messages(
            messages=messages,
            thread_id="thread-1",
            project_id="project-1",
            db_client=db_client,
        )

        assistant_inserts = [
            row for row in db_client.messages_table.inserts if row["type"] == "assistant"
        ]
        assert len(assistant_inserts) == 1
        persisted_content = json.loads(assistant_inserts[0]["content"])
        persisted_metadata = json.loads(assistant_inserts[0]["metadata"])

        assert persisted_content == {"role": "assistant", "content": "Hello world"}
        assert persisted_metadata["stream_status"] == "complete"
        assert persisted_metadata["thread_run_id"] == "run-1"
        assert persisted_metadata["source"] == "claude_sdk_aggregate"

    @pytest.mark.asyncio
    async def test_persist_run_messages_does_not_duplicate_renderable_final_assistant(self):
        """If the SDK final assistant is renderable, chunks should not create a duplicate row."""
        db_client = FakeDbClient()
        messages = [
            {
                "type": "assistant",
                "message_id": None,
                "is_llm_message": True,
                "content": json.dumps({"role": "assistant", "content": "Hello"}),
                "metadata": json.dumps({"stream_status": "chunk", "thread_run_id": "run-1"}),
            },
            {
                "type": "assistant",
                "message_id": "final",
                "is_llm_message": True,
                "content": json.dumps({"role": "assistant", "content": "Hello"}),
                "metadata": json.dumps({"stream_status": "complete", "thread_run_id": "run-1"}),
            },
        ]

        await _persist_run_messages(
            messages=messages,
            thread_id="thread-1",
            project_id="project-1",
            db_client=db_client,
        )

        assistant_inserts = [
            row for row in db_client.messages_table.inserts if row["type"] == "assistant"
        ]
        assert len(assistant_inserts) == 1
        persisted_metadata = json.loads(assistant_inserts[0]["metadata"])
        assert persisted_metadata.get("source") != "claude_sdk_aggregate"

    def test_local_claude_bridge_exposes_workspace_dir(self):
        """LocalClaudeBridge.workspace_dir is set from constructor cwd."""
        from agentscope_integration.claude_sdk_bridge import LocalClaudeBridge

        bridge = LocalClaudeBridge(
            deepseek_api_key="sk-test",
            cwd="/my/workspace",
        )
        assert bridge.workspace_dir == "/my/workspace"


    @pytest.mark.asyncio
    async def test_local_mode_bridge_uses_run_scoped_workspace(self, monkeypatch):
        """Claude SDK local mode must isolate each run in its own cwd."""
        from agentscope_integration.claude_sdk_bridge import LocalClaudeBridge

        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("CLAUDE_LOCAL_MODE", "1")
        monkeypatch.setenv("CLAUDE_LOCAL_WORKSPACE_ROOT", "/tmp/william-test-local")

        runner = ClaudeSDKRunner(
            thread_id="thread/unsafe",
            project_id="project:unsafe",
            model_key="claude-sdk",
            db_client=FakeDbClient(),
        )

        bridge = await runner._get_bridge(thread_run_id="run:unsafe/1")

        assert isinstance(bridge, LocalClaudeBridge)
        assert bridge.workspace_dir.startswith("/tmp/william-test-local/runs/")
        assert "run-unsafe-1" in bridge.workspace_dir
        assert "thread" not in bridge.workspace_dir.replace("/tmp/william-test-local/runs/", "")
        assert bridge.workspace_dir != "/tmp/claude-test-workspace"

    @pytest.mark.asyncio
    async def test_distinct_local_runs_get_distinct_workspaces(self, monkeypatch):
        """Two local runs must not share a physical execution workspace."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("CLAUDE_LOCAL_MODE", "1")
        monkeypatch.setenv("CLAUDE_LOCAL_WORKSPACE_ROOT", "/tmp/william-test-local")

        runner = ClaudeSDKRunner(
            thread_id="thread-1",
            project_id="project-1",
            model_key="claude-sdk",
            db_client=FakeDbClient(),
        )

        first = await runner._get_bridge(thread_run_id="run-a")
        second = await runner._get_bridge(thread_run_id="run-b")

        assert first.workspace_dir != second.workspace_dir
        assert first.workspace_dir.endswith("/run-a")
        assert second.workspace_dir.endswith("/run-b")

    @pytest.mark.asyncio
    async def test_sandbox_bridge_uses_claude_template_env(self, monkeypatch):
        """Production Claude SDK bridge should use configured full PPIO template."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
        monkeypatch.setenv("CLAUDE_LOCAL_MODE", "0")
        monkeypatch.setenv("CLAUDE_SANDBOX_TEMPLATE", "claude-agent-full-v1")
        monkeypatch.setenv("CLAUDE_SANDBOX_TEMPLATE_ID", "exgl29qnqh3oads1j4tz")

        runner = ClaudeSDKRunner(
            thread_id="thread-1",
            project_id="project-1",
            model_key="claude-sdk",
            db_client=FakeDbClient(),
        )

        bridge = await runner._get_bridge(thread_run_id="run-1")

        assert bridge._template_name == "claude-agent-full-v1"
        assert bridge._template_id == "exgl29qnqh3oads1j4tz"

    def test_sandbox_claude_bridge_has_default_workspace_dir(self):
        """SandboxClaudeBridge.workspace_dir defaults to /workspace."""
        from agentscope_integration.claude_sdk_bridge import SandboxClaudeBridge

        bridge = SandboxClaudeBridge(deepseek_api_key="sk-test")
        assert bridge.workspace_dir == "/workspace"

    @pytest.mark.asyncio
    async def test_run_rehydrates_thread_workspace_before_bridge_start(self, runner):
        """Claude SDK runs should inherit durable files from earlier runs in the same thread."""
        bridge = FakeSandboxBridge([_make_result()])
        bridge.workspace_dir = "/workspace"
        bridge.write_workspace_file_bytes = AsyncMock()
        bridge.make_workspace_dir = AsyncMock()
        events = []

        async def fake_rehydrate_thread(**kwargs):
            events.append(("rehydrate", kwargs["thread_id"]))
            await kwargs["make_dir"]("/workspace")
            await kwargs["write_file"]("/workspace/first.md", b"from first run")
            return {"total": 1, "rehydrated": 1, "failed": 0, "failures": []}

        async def start_with_order(*args, **kwargs):
            events.append(("start", None))
            return await FakeSandboxBridge.start(bridge, *args, **kwargs)

        bridge.start = AsyncMock(side_effect=start_with_order)

        with patch.object(runner, "_get_bridge", return_value=bridge), patch(
            "services.workspace_artifacts.workspace_artifacts.rehydrate_thread",
            side_effect=fake_rehydrate_thread,
        ):
            chunks = []
            async for chunk in runner.run(user_message="continue", thread_run_id="run-2"):
                chunks.append(chunk)

        assert events[0] == ("rehydrate", "thread-1")
        assert events[1] == ("start", None)
        bridge.make_workspace_dir.assert_awaited_with("/workspace")
        bridge.write_workspace_file_bytes.assert_awaited_with("/workspace/first.md", b"from first run")
        assert chunks[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_local_claude_bridge_rehydrate_rejects_paths_outside_workspace(tmp_path):
    from agentscope_integration.claude_sdk_bridge import LocalClaudeBridge

    bridge = LocalClaudeBridge(deepseek_api_key="sk-test", cwd=str(tmp_path))

    await bridge.write_workspace_file_bytes("/workspace/safe/file.txt", b"ok")
    assert (tmp_path / "safe" / "file.txt").read_bytes() == b"ok"

    with pytest.raises(ValueError):
        await bridge.write_workspace_file_bytes("/workspace/../escape.txt", b"bad")
    assert not (tmp_path.parent / "escape.txt").exists()
