"""Diagnose DB persist in prune_old_tool_outputs."""
import asyncio, json, os, sys, uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(override=True)

os.environ['AGENTSCOPE_MICRO_COMPACT_ENABLED'] = 'true'

from services.postgresql import DBConnection
from agentscope_integration.memory.messages_memory import MessagesTableMemory
from agentscope_integration.memory.micro_compact import PRUNE_PROTECT, PRUNE_MINIMUM, prune_tool_outputs


async def main():
    db = DBConnection()
    client = await db.client

    # Find a thread to use
    result = await client.table('projects').select('project_id').order('created_at', desc=True).limit(1).execute()
    project_id = result.data[0]['project_id']
    result = await client.table('threads').select('thread_id').eq('project_id', project_id).limit(1).execute()
    thread_id = result.data[0]['thread_id']
    print(f"Using thread: {thread_id}")

    # Create MessagesTableMemory instance
    mem = MessagesTableMemory(
        db_client=client,
        thread_id=thread_id,
        project_id=project_id,
        enable_micro_compact=True,
    )

    # Insert fake tool messages directly via DB (bypass Msg requirement)
    big_data = "X" * 2000
    inserted_ids = []
    for i in range(10):
        data = {
            'thread_id': thread_id,
            'project_id': project_id,
            'type': 'tool',
            'role': 'assistant',
            'content': json.dumps({
                "tool_name": "execute_command",
                "result": f"output_{i}: {big_data}",
            }),
            'metadata': json.dumps({"epoch": 1}),
        }
        result = await client.table('messages').insert(data)
        if result.data:
            inserted_ids.append(result.data[0].get('message_id'))
    print(f"Inserted {len(inserted_ids)} tool messages")

    # Read them back
    result = await client.table('messages').select('*').eq('thread_id', thread_id).eq('type', 'tool').order('created_at', desc=False).execute()
    rows = result.data or []
    print(f"DB has {len(rows)} tool rows")
    for r in rows[:2]:
        mid = str(r.get('message_id'))[:16] if r.get('message_id') else 'NONE'
        print(f"  row: message_id={mid}... meta={str(r.get('metadata'))[:60]}...")

    # Step 1: Direct prune_tool_outputs test (in-memory only)
    active_rows = []
    for r in rows:
        meta = r.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        if isinstance(meta, dict) and meta.get("time_compacted"):
            continue
        active_rows.append(r)

    print(f"\nActive rows (no time_compacted): {len(active_rows)}")
    pruned = prune_tool_outputs(active_rows, protect=PRUNE_PROTECT, minimum=PRUNE_MINIMUM)
    _marked = sum(1 for r in pruned if isinstance(r.get("metadata"), dict) and r["metadata"].get("time_compacted"))
    print(f"After prune_tool_outputs: {_marked}/{len(pruned)} marked")

    # Step 2: Full prune_old_tool_outputs (includes DB persist)
    print("\n--- Testing prune_old_tool_outputs() ---")
    result_count = await mem.prune_old_tool_outputs()
    print(f"Returned: {result_count}")

    # Step 3: Re-read from DB and check
    result = await client.table('messages').select('*').eq('thread_id', thread_id).eq('type', 'tool').order('created_at', desc=False).execute()
    rows = result.data or []
    compacted = 0
    for r in rows:
        meta = r.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                pass
        if isinstance(meta, dict) and meta.get("time_compacted"):
            compacted += 1
    print(f"\nFINAL: {compacted}/{len(rows)} rows have time_compacted in DB")
    if compacted > 0:
        print("SUCCESS: DB persist works!")
    else:
        print("FAIL: No rows persisted to DB")


asyncio.run(main())
