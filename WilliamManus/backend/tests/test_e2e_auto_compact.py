"""E2E Auto Compact test — verifies compaction trigger with lowered threshold."""
import asyncio, json, os, sys, time, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from dotenv import load_dotenv
load_dotenv(override=True)

# Enable Auto Compact with very low trigger for testing
os.environ['AGENTSCOPE_MICRO_COMPACT_ENABLED'] = 'true'
os.environ['AGENTSCOPE_AUTO_COMPACT_ENABLED'] = 'true'
os.environ['AGENTSCOPE_AUTO_COMPACT_TRIGGER_TOKENS'] = '5000'
os.environ['AGENTSCOPE_AUTO_COMPACT_MODEL'] = 'deepseek-v4-flash'
os.environ['AGENTSCOPE_AUTO_COMPACT_SHADOW_MODE'] = 'true'  # shadow first

from services.postgresql import DBConnection
from agentscope_integration import AgentScopeRunner


async def main():
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
        print("FAIL: No sandbox project"); return

    result = await client.table('threads').select('thread_id').eq(
        'project_id', project_id).limit(1).execute()
    thread_id = result.data[0]['thread_id']
    print(f"Thread: {thread_id[:12]}...")

    runner = AgentScopeRunner(
        thread_id=thread_id, project_id=project_id,
        model_key='minimax-m2.5', db_client=client,
    )
    await runner.setup()

    # Verify auto compact is registered
    agent = runner.orchestrator.agent
    hooks = getattr(agent, '_instance_post_reasoning_hooks', {})
    print(f"Auto Compact hook registered: {'auto_compact' in hooks}")
    assert 'auto_compact' in hooks, "Auto Compact hook NOT registered!"

    mgr = getattr(runner, '_auto_compact_manager', None)
    print(f"AutoCompactManager: {'present' if mgr else 'MISSING'}")
    if mgr:
        print(f"  shadow_mode: {mgr.config.shadow_mode}")
        print(f"  trigger_tokens: {mgr.config.trigger_tokens}")
        print(f"  compact_model: {mgr.config.compact_model_key}")

    # Simple task to trigger LLM call
    task = "Say hello and tell me what 2+2 is. Keep it brief."
    thread_run_id = str(uuid.uuid4())
    start = time.time()
    chunks = 0
    async for chunk in runner.run(task, thread_run_id):
        chunks += 1
        if time.time() - start > 60:
            print("TIMEOUT")
            break

    print(f"Done: {chunks} chunks in {time.time()-start:.1f}s")

    # Check auto compact state
    if mgr:
        print(f"  consecutive_failures: {mgr._consecutive_failures}")
        print(f"  circuit_open: {mgr.circuit_open}")

    await runner.close()

    # Now test with shadow_mode OFF
    print("\n--- Testing with shadow_mode=False ---")
    os.environ['AGENTSCOPE_AUTO_COMPACT_SHADOW_MODE'] = 'false'

    runner2 = AgentScopeRunner(
        thread_id=thread_id, project_id=project_id,
        model_key='minimax-m2.5', db_client=client,
    )
    await runner2.setup()

    mgr2 = getattr(runner2, '_auto_compact_manager', None)
    if mgr2:
        print(f"  shadow_mode: {mgr2.config.shadow_mode}")

    task2 = "Say 'hello world' and tell me what 3+3 is."
    thread_run_id2 = str(uuid.uuid4())
    start = time.time()
    chunks2 = 0
    async for chunk in runner2.run(task2, thread_run_id2):
        chunks2 += 1
        if time.time() - start > 120:
            print("TIMEOUT")
            break

    print(f"Done: {chunks2} chunks in {time.time()-start:.1f}s")

    if mgr2:
        print(f"  consecutive_failures: {mgr2._consecutive_failures}")
        print(f"  circuit_open: {mgr2.circuit_open}")

    # Check DB for compaction messages
    result = await client.table('messages').select('*').eq(
        'thread_id', thread_id).order('created_at', desc=True).limit(50).execute()
    rows = result.data or []
    compact_rows = [r for r in rows if isinstance(r.get('metadata'), str) and
                    '"compaction":true' in r.get('metadata','')]
    summary_rows = [r for r in rows if isinstance(r.get('metadata'), str) and
                    '"summary":true' in r.get('metadata','')]
    print(f"  Compaction messages: {len(compact_rows)}")
    print(f"  Summary messages: {len(summary_rows)}")

    await runner2.close()

    print("\n*** AUTO COMPACT E2E COMPLETE ***")


asyncio.run(main())
