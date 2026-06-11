"""
E2E test: Micro Compact with real sandbox and database.

1. Runs the agent through multiple tool-calling turns (read files, execute commands)
2. Verifies that ``time_compacted`` metadata appears on old tool output rows
3. Confirms skill outputs (run_skill_script) are NEVER compacted

Usage:
    cd WilliamManus/backend
    AGENTSCOPE_MICRO_COMPACT_ENABLED=true \\
    AGENTSCOPE_AUTO_COMPACT_ENABLED=false \\
    python -m pytest tests/test_e2e_micro_compact.py -v -s
"""

import asyncio
import json
import os
import sys
import uuid
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")

from dotenv import load_dotenv

load_dotenv(override=True)

# Force-enable Micro Compact, disable Auto Compact for this test
os.environ.setdefault("AGENTSCOPE_MICRO_COMPACT_ENABLED", "true")
os.environ.setdefault("AGENTSCOPE_AUTO_COMPACT_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_or_create_test_project(db_client):
    """Find a project with an active sandbox, or return the first project."""
    result = await db_client.table("projects") \
        .select("project_id, name, sandbox") \
        .order("created_at", desc=True) \
        .limit(10) \
        .execute()

    for p in result.data or []:
        sandbox = p.get("sandbox", "{}")
        if isinstance(sandbox, str):
            try:
                sandbox = json.loads(sandbox)
            except Exception:
                sandbox = {}
        if isinstance(sandbox, dict) and sandbox.get("id"):
            return p["project_id"], p.get("name", "e2e-test")

    if result.data:
        return result.data[0]["project_id"], result.data[0].get("name", "e2e-test")
    return None, None


def _count_compacted_tool_rows(rows):
    """Count how many tool rows have a truthy time_compacted marker."""
    count = 0
    for r in rows:
        meta = r.get("metadata", {})
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if isinstance(meta, dict) and meta.get("time_compacted"):
            count += 1
    return count


# ---------------------------------------------------------------------------
# E2E Tests
# ---------------------------------------------------------------------------


class TestMicroCompactE2E:
    """Run the agent with multiple tool calls and verify Micro Compact."""

    @pytest.mark.asyncio
    async def test_micro_compact_prunes_old_tool_outputs(self):
        """
        1. Create a fresh thread
        2. Ask the agent to do multiple file operations (read, write, list)
        3. Let Micro Compact run via the pre_reasoning hook
        4. Query the DB for tool rows — verify old ones are compacted
        5. Verify skill results are NOT compacted
        """
        from services.postgresql import DBConnection
        from agentscope_integration import AgentScopeRunner

        db = DBConnection()
        client = await db.client

        # Find a project with a sandbox
        project_id, project_name = await _get_or_create_test_project(client)
        if not project_id:
            pytest.skip("No project with sandbox available")

        # Create a new thread for this test
        import datetime
        thread_result = await client.table("threads").insert({
            "project_id": project_id,
            "title": f"E2E Micro Compact Test {uuid.uuid4().hex[:8]}",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }).execute()
        thread_id = thread_result.data[0]["thread_id"] if thread_result.data else None
        if not thread_id:
            pytest.skip("Could not create test thread")

        print(f"\n  Project: {project_id[:8]}... ({project_name})")
        print(f"  Thread:  {thread_id[:8]}...")

        # Create runner with our hooks
        runner = AgentScopeRunner(
            thread_id=thread_id,
            project_id=project_id,
            model_key="gemini-3-flash",
            db_client=client,
        )
        await runner.setup()

        # Verify hooks are registered
        agent = runner.orchestrator.agent
        mc_hooks = getattr(agent, "_instance_pre_reasoning_hooks", {})
        assert "micro_compact" in mc_hooks, "Micro Compact hook not registered!"
        print(f"  Micro Compact hook: REGISTERED")

        # Task: ask agent to do multiple file operations
        task = (
            "Please do the following file operations in the sandbox /workspace directory:\n"
            "1. Create a file called /workspace/test1.txt with 'Hello World 1'\n"
            "2. Create a file called /workspace/test2.txt with 'Hello World 2'\n"
            "3. Create a file called /workspace/test3.txt with 'Hello World 3'\n"
            "4. Read all three files to confirm they were created\n"
            "5. Run the command 'ls -la /workspace/test*.txt'\n"
            "6. Create a file called /workspace/test4.txt with 'Hello World 4'\n"
            "7. Read /workspace/test4.txt\n"
            "Respond briefly after each step. Maximum 5 sentences total."
        )

        print(f"  Task: {task[:100]}...")

        thread_run_id = str(uuid.uuid4())
        start = time.time()
        chunks = 0
        final_text = ""

        try:
            async for chunk in runner.run(task, thread_run_id):
                chunks += 1
                c = chunk.get("content", "")
                if isinstance(c, str):
                    try:
                        data = json.loads(c)
                        t = data.get("content", "")
                        if t and isinstance(t, str):
                            final_text = t
                    except Exception:
                        pass

                # Abort after 60s
                if time.time() - start > 60:
                    print("  ⚠️ Timeout after 60s — stopping")
                    break

            elapsed = time.time() - start
            print(f"  Agent completed in {elapsed:.1f}s, {chunks} chunks")
            print(f"  Response: {final_text[:200]}")

        finally:
            await runner.close()

        # --- VERIFY: check DB for compacted tool rows ---
        result = await client.table("messages") \
            .select("*") \
            .eq("thread_id", thread_id) \
            .eq("type", "tool") \
            .order("created_at", desc=False) \
            .execute()

        tool_rows = result.data or []
        compacted = _count_compacted_tool_rows(tool_rows)

        print(f"\n  DB tool rows: {len(tool_rows)} total, {compacted} compacted")

        # Diagnostic: list tool names and their compacted status
        for r in tool_rows:
            c = r.get("content", {})
            if isinstance(c, str) and c.startswith("{"):
                try:
                    c = json.loads(c)
                except Exception:
                    pass
            name = c.get("tool_name", "?") if isinstance(c, dict) else "?"
            meta = r.get("metadata", {})
            if isinstance(meta, str) and meta.startswith("{"):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            is_compacted = bool(
                isinstance(meta, dict) and meta.get("time_compacted")
            )
            print(f"    [{name:25s}] compacted={is_compacted}")

        # --- ASSERTIONS ---
        assert len(tool_rows) > 3, (
            f"Expected >3 tool calls, got {len(tool_rows)}"
        )

        # At least some old tool outputs should be compacted
        # (PRUNE_PROTECT=3000 chars protects the most recent ones)
        assert compacted > 0 or len(tool_rows) <= 6, (
            f"Micro Compact should have pruned some old tool outputs! "
            f"({len(tool_rows)} total, {compacted} compacted). "
            f"If <=6 rows, all may be within protect zone."
        )

        # Skills must NEVER be compacted
        for r in tool_rows:
            c = r.get("content", {})
            if isinstance(c, str) and c.startswith("{"):
                try:
                    c = json.loads(c)
                except Exception:
                    pass
            name = c.get("tool_name", "") if isinstance(c, dict) else ""
            if "skill" in name.lower():
                meta = r.get("metadata", {})
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                assert not (isinstance(meta, dict) and meta.get("time_compacted")), (
                    f"Skill tool '{name}' was compacted — this is FORBIDDEN!"
                )
                print(f"    [{name:25s}] SKILL — correctly NOT compacted")
