"""Sandbox clone renewal: clones paused sandboxes approaching the 30-day expiry."""

import json
from datetime import datetime, timezone, timedelta

from utils.logger import logger
from sandbox.sandbox import clone_sandbox, pause_sandbox, delete_sandbox


async def renew_expiring_sandboxes(db_client, max_age_days: int = 29) -> dict:
    """Scan all projects. Clone sandboxes paused >= max_age_days ago.

    Returns {"renewed": [...], "failed": [...], "skipped": int}.
    """
    renewed = []
    failed = []
    skipped = 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    result = await db_client.table('projects').select('project_id, sandbox').execute()
    if not result.data:
        return {"renewed": renewed, "failed": failed, "skipped": 0}

    for row in result.data:
        project_id = row.get('project_id')
        raw = row.get('sandbox', '{}')
        sandbox_info = json.loads(raw) if isinstance(raw, str) else (raw or {})

        if sandbox_info.get('state') != 'paused':
            skipped += 1
            continue

        paused_at_str = sandbox_info.get('paused_at')
        if not paused_at_str:
            skipped += 1
            continue

        try:
            paused_at = datetime.fromisoformat(paused_at_str)
        except (ValueError, TypeError):
            skipped += 1
            continue

        if paused_at > cutoff:
            skipped += 1
            continue

        old_id = sandbox_info.get('id')
        if not old_id:
            skipped += 1
            continue

        try:
            new_id, snapshot_template_id = await clone_sandbox(old_id, count=1, timeout=3600)

            # Pause the new clone immediately
            await pause_sandbox(new_id)

            # Update DB to point to the new clone
            now_iso = datetime.now(timezone.utc).isoformat()
            updated_info = {
                **sandbox_info,
                'id': new_id,
                'state': 'paused',
                'paused_at': now_iso,
                'cloned_from': old_id,
                'snapshot_template_id': snapshot_template_id,
            }
            await db_client.table('projects').update({
                'sandbox': json.dumps(updated_info)
            }).eq('project_id', project_id).execute()

            # Kill old sandbox
            try:
                await delete_sandbox(old_id)
            except Exception as del_err:
                logger.warning(f"Failed to delete old sandbox {old_id}: {del_err}")

            renewed.append({"project_id": project_id, "old_id": old_id, "new_id": new_id})
            logger.info(f"Renewed sandbox for project {project_id}: {old_id} -> {new_id}")

        except Exception as e:
            failed.append({"project_id": project_id, "sandbox_id": old_id, "error": str(e)})
            logger.error(f"Failed to renew sandbox for project {project_id}: {e}")

    return {"renewed": renewed, "failed": failed, "skipped": skipped}
