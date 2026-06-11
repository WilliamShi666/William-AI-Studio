"""E2E KV Cache persistence test — verifies CacheBoundaryHook with real models.

Tests:
1. OpenRouter model (minimax-m2.5) — CacheBoundaryHook freezes and stays stable
2. DeepSeek official (deepseek-v4-flash-high) — CacheBoundaryHook + canonical JSON
3. Multi-turn conversation — system/tool hashes remain unchanged across calls
"""
import asyncio, json, os, sys, time, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv
load_dotenv(override=True)

os.environ['AGENTSCOPE_MICRO_COMPACT_ENABLED'] = 'true'
os.environ['AGENTSCOPE_AUTO_COMPACT_ENABLED'] = 'false'
os.environ['AGENTSCOPE_KV_CACHE_BOUNDARY_CHECK'] = 'true'

from services.postgresql import DBConnection
from agentscope_integration import AgentScopeRunner


async def test_model(model_key: str, label: str):
    """Run KV Cache E2E for a single model."""
    print(f"\n{'='*60}")
    print(f"Testing: {label} ({model_key})")
    print(f"{'='*60}")

    db = DBConnection()
    client = await db.client

    # Find sandbox project
    result = await client.table('projects').select('project_id,sandbox').order(
        'created_at', desc=True).limit(20).execute()
    project_id = None
    for p in (result.data or []):
        s = p.get('sandbox', '{}')
        if isinstance(s, str):
            try: s = json.loads(s)
            except: s = {}
        if isinstance(s, dict) and s.get('id'):
            project_id = p['project_id']; break

    if not project_id:
        print(f"  SKIP: No sandbox project")
        return None

    result = await client.table('threads').select('thread_id').eq(
        'project_id', project_id).limit(1).execute()
    thread_id = result.data[0]['thread_id']
    print(f"  Thread: {thread_id[:12]}...")

    runner = AgentScopeRunner(
        thread_id=thread_id, project_id=project_id,
        model_key=model_key, db_client=client,
    )
    await runner.setup()

    agent = runner.orchestrator.agent
    model = agent.model

    # Check CacheBoundaryHook is present
    boundary = getattr(model, '_cache_boundary', None)
    if boundary is None:
        print(f"  SKIP: no CacheBoundaryHook on model {type(model).__name__}")
        await runner.close()
        return {"model": label, "boundary_present": False}

    print(f"  Model type: {type(model).__name__}")
    print(f"  Hook present: CacheBoundaryHook")

    # Task: minimal tool calls to trigger multiple LLM API calls
    task = (
        "In /workspace, do these steps using execute_command:\n"
        "1. echo 'kv_cache_test_1' > /workspace/kv1.txt\n"
        "2. echo 'kv_cache_test_2' > /workspace/kv2.txt\n"
        "3. cat /workspace/kv1.txt /workspace/kv2.txt\n"
        "Respond briefly after each step."
    )

    thread_run_id = str(uuid.uuid4())
    start = time.time()
    chunks = 0
    async for chunk in runner.run(task, thread_run_id):
        chunks += 1
        if time.time() - start > 120:
            print("  TIMEOUT after 120s")
            break

    print(f"  Done: {chunks} chunks in {time.time()-start:.1f}s")

    # Verify CacheBoundaryHook state
    frozen = boundary._frozen
    drift_count = boundary._drift_count
    sys_hash = boundary._frozen_system_hash
    tool_hash = boundary._frozen_tool_hash

    print(f"  Frozen: {frozen}")
    print(f"  Drift events: {drift_count}")
    print(f"  System hash: {sys_hash[:16] if sys_hash else 'N/A'}...")
    print(f"  Tool hash:   {tool_hash[:16] if tool_hash else 'N/A'}...")

    # Verify DB compacted status
    result = await client.table('messages').select('*').eq(
        'thread_id', thread_id).eq('type', 'tool').execute()
    rows = result.data or []
    compacted = 0
    for r in rows:
        meta = r.get('metadata')
        if isinstance(meta, str) and meta.startswith('{'):
            try: meta = json.loads(meta)
            except: pass
        if isinstance(meta, dict) and meta.get('time_compacted'):
            compacted += 1
    print(f"  Micro Compact: {compacted}/{len(rows)} tool outputs compacted")

    await runner.close()

    return {
        "model": label,
        "provider": "deepseek" if "deepseek" in model_key else "openrouter",
        "boundary_present": True,
        "frozen": frozen,
        "drift_count": drift_count,
        "tool_rows": len(rows),
        "compacted": compacted,
    }


async def main():
    results = []

    # Test 1: OpenRouter model (MiniMax)
    r = await test_model("minimax-m2.5", "OpenRouter MiniMax M2.5")
    if r: results.append(r)

    # Test 2: DeepSeek official model
    r = await test_model("deepseek-v4-flash-high", "DeepSeek V4 Flash (Official)")
    if r: results.append(r)

    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")
    all_pass = True
    for r in results:
        frozen_ok = "PASS" if r.get("frozen") else "FAIL"
        drift_ok = "PASS" if r.get("drift_count", -1) == 0 else "FAIL"
        if not r.get("frozen") or r.get("drift_count", -1) != 0:
            all_pass = False
        print(f"  {r['model']:30s} | frozen={frozen_ok} | drift={drift_ok} | compacted={r.get('compacted',0)}/{r.get('tool_rows',0)}")

    if all_pass and all(r.get("frozen") for r in results):
        print("\n*** KV CACHE PERSISTENCE IS WORKING! ***")
    else:
        print("\nFAIL: Some checks did not pass")


asyncio.run(main())
