"""Run E2E Micro Compact test — creates tool calls, verifies pruning."""
import asyncio, json, os, sys, time, uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dotenv import load_dotenv
load_dotenv(override=True)

os.environ['AGENTSCOPE_MICRO_COMPACT_ENABLED'] = 'true'
os.environ['AGENTSCOPE_AUTO_COMPACT_ENABLED'] = 'false'

from services.postgresql import DBConnection
from agentscope_integration import AgentScopeRunner


async def main():
    db = DBConnection()
    client = await db.client

    # Find sandbox project
    result = await client.table('projects').select('project_id,sandbox').order('created_at', desc=True).limit(20).execute()
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

    result = await client.table('threads').select('thread_id').eq('project_id', project_id).limit(1).execute()
    thread_id = result.data[0]['thread_id']
    print(f"Thread: {thread_id[:12]}...")

    runner = AgentScopeRunner(
        thread_id=thread_id, project_id=project_id,
        model_key='gemini-3-flash', db_client=client,
    )
    await runner.setup()

    agent = runner.orchestrator.agent
    hooks = getattr(agent, '_instance_pre_reasoning_hooks', {})
    assert 'micro_compact' in hooks, "Micro Compact hook NOT registered!"
    print("Hook: REGISTERED")

    task = (
        "In /workspace, do ALL of these steps using execute_command:\n"
        "1. Create /workspace/f1.txt with content 'data111'\n"
        "2. Create /workspace/f2.txt with content 'data222'\n"
        "3. Create /workspace/f3.txt with content 'data333'\n"
        "4. Create /workspace/f4.txt with content 'data444'\n"
        "5. Create /workspace/f5.txt with content 'data555'\n"
        "6. Create /workspace/f6.txt with content 'data666'\n"
        "7. Create /workspace/f7.txt with content 'data777'\n"
        "8. Create /workspace/f8.txt with content 'data888'\n"
        "9. Read all 8 files\n"
        "10. List all files: ls -la /workspace/f*.txt\n"
        "Respond briefly after each operation."
    )

    thread_run_id = str(uuid.uuid4())
    start = time.time()
    async for chunk in runner.run(task, thread_run_id):
        if time.time() - start > 120:
            print("TIMEOUT"); break

    print(f"Agent done: {time.time()-start:.1f}s")
    await runner.close()

    # Check DB
    result = await client.table('messages').select('*').eq('thread_id', thread_id).eq('type', 'tool').execute()
    rows = result.data or []
    compacted = 0
    for r in rows:
        c = r.get('content', '{}')
        if isinstance(c, str) and c.startswith('{'):
            try: c = json.loads(c)
            except: pass
        name = c.get('tool_name','?') if isinstance(c, dict) else '?'
        meta = r.get('metadata', '{}')
        if isinstance(meta, str) and meta.startswith('{'):
            try: meta = json.loads(meta)
            except: pass
        is_c = isinstance(meta, dict) and bool(meta.get('time_compacted'))
        if is_c: compacted += 1
        print(f"  [{name:25s}] {'COMPACTED' if is_c else ''}")

    print(f"\nRESULT: {compacted}/{len(rows)} compacted")

    # Skills must NEVER be compacted
    for r in rows:
        c = r.get('content', '{}')
        if isinstance(c, str) and c.startswith('{'):
            try: c = json.loads(c)
            except: pass
        name = c.get('tool_name','') if isinstance(c, dict) else ''
        meta = r.get('metadata', '{}')
        if isinstance(meta, str) and meta.startswith('{'):
            try: meta = json.loads(meta)
            except: pass
        if 'skill' in name.lower() or name in ('get_available_skills','load_skill','run_skill_script'):
            assert not (isinstance(meta, dict) and meta.get('time_compacted')), f"SKILL {name} compacted!"
    print("Skills check: PASS")

    if compacted > 0:
        print("\n*** MICRO COMPACT IS WORKING! ***")
    elif len(rows) <= 8:
        print(f"\nWARNING: 0/{len(rows)} compacted — may all be within 3000-char protect zone")
    else:
        print(f"\nFAIL: 0/{len(rows)} compacted")

asyncio.run(main())
