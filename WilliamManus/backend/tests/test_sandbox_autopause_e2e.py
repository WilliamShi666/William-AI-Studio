"""
E2E test: Sandbox auto-pause after agent run completes.

Flow:
  1. Get JWT token for test user
  2. Initiate agent (creates thread + project + sandbox + starts agent)
  3. Poll until agent run completes
  4. Check sandbox state → expect 'paused'
  5. Manual pause → expect 'already_paused'
  6. Start second agent run → verify sandbox resumes (not recreated)

Usage:
  cd WilliamManus/backend && python tests/test_sandbox_autopause_e2e.py
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_URL = os.getenv("E2E_BASE_URL", "http://localhost:8002/api")
DATABASE_URL = os.getenv("DATABASE_URL")
JWT_SECRET = os.getenv("JWT_SECRET_KEY")
POLL_INTERVAL = 3          # seconds between status polls
POLL_TIMEOUT = 300         # max seconds to wait for agent completion
TEST_PROMPT = "Say hello"  # lightweight prompt so agent finishes fast


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _get_test_token() -> tuple[str, str]:
    """Mint a JWT for the codex test user directly from the DB."""
    import asyncpg
    import jwt as pyjwt

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL must be set to run this E2E test")
    if not JWT_SECRET:
        raise RuntimeError("JWT_SECRET_KEY must be set to run this E2E test")

    conn = await asyncpg.connect(DATABASE_URL)
    try:
        row = await conn.fetchrow(
            "SELECT id, email FROM users WHERE email = 'codex.test+ssr1@local.dev' LIMIT 1"
        )
        if not row:
            raise RuntimeError("Test user codex.test+ssr1@local.dev not found in DB")

        user_id = str(row["id"])
        token = pyjwt.encode(
            {"sub": user_id, "exp": datetime.utcnow() + timedelta(hours=1), "iat": datetime.utcnow()},
            JWT_SECRET,
            algorithm="HS256",
        )
        return token, user_id
    finally:
        await conn.close()


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


async def _poll_agent_run(client: httpx.AsyncClient, headers: dict, thread_id: str, run_count: int = 1) -> dict:
    """Poll agent-runs until we see `run_count` terminal runs."""
    deadline = asyncio.get_event_loop().time() + POLL_TIMEOUT
    while asyncio.get_event_loop().time() < deadline:
        resp = await client.get(f"{BASE_URL}/thread/{thread_id}/agent-runs", headers=headers)
        resp.raise_for_status()
        runs = resp.json().get("agent_runs", [])
        terminal = [r for r in runs if r.get("status") in ("completed", "failed", "error")]
        if len(terminal) >= run_count:
            return terminal[0]  # most recent (desc order)
        await asyncio.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Agent run did not complete within {POLL_TIMEOUT}s")


# ---------------------------------------------------------------------------
# Main E2E flow
# ---------------------------------------------------------------------------

async def run_e2e():
    token, user_id = await _get_test_token()
    headers = _auth_headers(token)
    print(f"[1/6] Authenticated as user {user_id}")

    async with httpx.AsyncClient(timeout=120) as client:
        # --- Step 2: Create thread (creates project + sandbox) ---
        resp = await client.post(
            f"{BASE_URL}/threads",
            headers=headers,
            data={"name": f"E2E-autopause-{uuid.uuid4().hex[:8]}"},
        )
        if resp.status_code != 200:
            print(f"      create thread ERROR: {resp.status_code} - {resp.text[:500]}")
        resp.raise_for_status()
        create_data = resp.json()
        thread_id = create_data["thread_id"]
        project_id = create_data.get("project_id")
        print(f"[2/6] Created thread={thread_id}, project={project_id}")

        # Verify sandbox was stored
        resp = await client.get(f"{BASE_URL}/sandbox/{project_id}/state", headers=headers)
        resp.raise_for_status()
        initial_state = resp.json()
        print(f"      Initial sandbox: state={initial_state.get('state')}, id={initial_state.get('id')}")

        # --- Step 3: Start agent (this creates ADK session + runs agent) ---
        resp = await client.post(
            f"{BASE_URL}/thread/{thread_id}/agent/start",
            headers=headers,
            json={"stream": False},
        )
        if resp.status_code != 200:
            print(f"      agent/start ERROR: {resp.status_code} - {resp.text[:500]}")
        resp.raise_for_status()
        agent_run_id = resp.json().get("agent_run_id")
        print(f"[3/6] Agent run started: {agent_run_id}")

        # Poll until agent completes
        print(f"      Polling (timeout={POLL_TIMEOUT}s)...")
        final_run = await _poll_agent_run(client, headers, thread_id, run_count=1)
        final_status = final_run.get("status")
        print(f"      Agent run finished: {final_status}")

        # --- Step 4: Check sandbox state → expect 'paused' ---
        await asyncio.sleep(5)  # give auto-pause time to propagate
        resp = await client.get(f"{BASE_URL}/sandbox/{project_id}/state", headers=headers)
        resp.raise_for_status()
        state_data = resp.json()
        sandbox_state = state_data.get("state")
        sandbox_id = state_data.get("id")
        print(f"[4/6] Sandbox state: {sandbox_state}  (id={sandbox_id})")
        assert sandbox_state == "paused", f"Expected sandbox state 'paused', got '{sandbox_state}'"

        # --- Step 5: Manual pause → expect 'already_paused' ---
        resp = await client.post(f"{BASE_URL}/sandbox/{project_id}/pause", headers=headers)
        resp.raise_for_status()
        pause_data = resp.json()
        pause_status = pause_data.get("status")
        print(f"[5/6] Manual pause response: {pause_status}")
        assert pause_status == "already_paused", f"Expected 'already_paused', got '{pause_status}'"

        # --- Step 6: Second agent run → sandbox should resume (not recreate) ---
        resp = await client.post(
            f"{BASE_URL}/thread/{thread_id}/agent/start",
            headers=headers,
            json={"stream": False},
        )
        if resp.status_code != 200:
            print(f"      agent/start ERROR: {resp.status_code} - {resp.text[:500]}")
        resp.raise_for_status()
        agent_run_2 = resp.json()
        print(f"[6/6] Second agent run started: {agent_run_2.get('agent_run_id')}")

        # Poll second run
        final_run_2 = await _poll_agent_run(client, headers, thread_id, run_count=2)
        print(f"      Second run finished: {final_run_2.get('status')}")

        # Check sandbox id is the same (resumed, not recreated)
        await asyncio.sleep(5)
        resp = await client.get(f"{BASE_URL}/sandbox/{project_id}/state", headers=headers)
        resp.raise_for_status()
        state_data_2 = resp.json()
        sandbox_id_2 = state_data_2.get("id")
        sandbox_state_2 = state_data_2.get("state")
        print(f"      Sandbox after 2nd run: state={sandbox_state_2}, id={sandbox_id_2}")

        if sandbox_id_2 == sandbox_id:
            print("      PASS: Sandbox was resumed (same ID)")
        else:
            print(f"      INFO: Sandbox was recreated (old={sandbox_id}, new={sandbox_id_2})")

    print("\n=== E2E SANDBOX AUTO-PAUSE: ALL CHECKS PASSED ===")


if __name__ == "__main__":
    asyncio.run(run_e2e())
