from typing import Optional, List, Dict, Any
from datetime import datetime, timezone, timedelta
from utils.cache import Cache
from utils.logger import logger
from utils.config import config
from services import redis


async def _cleanup_redis_response_list(agent_run_id: str):
    try:
        response_list_key = f"agent_run:{agent_run_id}:responses"
        await redis.delete(response_list_key)
        logger.debug(f"Cleaned up Redis response list for agent run {agent_run_id}")
    except Exception as e:
        logger.warning(f"Failed to clean up Redis response list for {agent_run_id}: {str(e)}")


async def check_for_active_project_agent_run(client, project_id: str):
    project_threads = await client.table('threads').select('thread_id').eq('project_id', project_id).execute()
    project_thread_ids = [t['thread_id'] for t in project_threads.data]

    if project_thread_ids:
        active_runs = await client.table('agent_runs').select('id').in_('thread_id', project_thread_ids).eq('status', 'running').execute()
        if active_runs.data and len(active_runs.data) > 0:
            return active_runs.data[0]['id']
    return None


async def stop_agent_run(db, agent_run_id: str, error_message: Optional[str] = None):
    """Legacy compatibility wrapper for callers that still import from agent.utils.

    Keep the historical signature to avoid breaking old code paths, but delegate to
    the actively maintained API stop implementation so stop semantics stay aligned.
    """
    logger.warning(
        "agent.utils.stop_agent_run is deprecated; delegating to agent.api.stop_agent_run for %s",
        agent_run_id,
    )
    from .api import stop_agent_run as api_stop_agent_run

    await api_stop_agent_run(agent_run_id, error_message=error_message)


async def check_agent_run_limit(client, account_id: str) -> Dict[str, Any]:
    """
    Check if the account has reached the limit of 3 parallel agent runs within the past 24 hours.
    
    Returns:
        Dict with 'can_start' (bool), 'running_count' (int), 'running_thread_ids' (list)
    """
    try:
        result = await Cache.get(f"agent_run_limit:{account_id}")
        if result:
            return result

        # Calculate 24 hours ago
        twenty_four_hours_ago = datetime.now(timezone.utc) - timedelta(hours=24)
        twenty_four_hours_ago_iso = twenty_four_hours_ago.isoformat()
        
        logger.debug(f"Checking agent run limit for account {account_id} since {twenty_four_hours_ago_iso}")
        
        # Get all threads for this account
        threads_result = await client.table('threads').select('thread_id').eq('account_id', account_id).execute()
        
        if not threads_result.data:
            logger.debug(f"No threads found for account {account_id}")
            return {
                'can_start': True,
                'running_count': 0,
                'running_thread_ids': []
            }
        
        thread_ids = [thread['thread_id'] for thread in threads_result.data]
        logger.debug(f"Found {len(thread_ids)} threads for account {account_id}")
        
        # Query for running agent runs within the past 24 hours for these threads
        running_runs_result = await client.table('agent_runs').select('id', 'thread_id', 'started_at').in_('thread_id', thread_ids).eq('status', 'running').gte('started_at', twenty_four_hours_ago_iso).execute()
        
        running_runs = running_runs_result.data or []
        running_count = len(running_runs)
        running_thread_ids = [run['thread_id'] for run in running_runs]
        
        logger.info(f"Account {account_id} has {running_count} running agent runs in the past 24 hours")
        
        result = {
            'can_start': running_count < config.MAX_PARALLEL_AGENT_RUNS,
            'running_count': running_count,
            'running_thread_ids': running_thread_ids
        }
        await Cache.set(f"agent_run_limit:{account_id}", result)
        return result

    except Exception as e:
        logger.error(f"Error checking agent run limit for account {account_id}: {str(e)}")
        # In case of error, allow the run to proceed but log the error
        return {
            'can_start': True,
            'running_count': 0,
            'running_thread_ids': []
        }


async def check_agent_count_limit(client, account_id: str) -> Dict[str, Any]:
    try:
        # 在本地模式下，允许创建几乎无限的定制Agent
        if config.ENV_MODE.value == "local":
            return {
                'can_create': True,
                'current_count': 0,  # 返回 0 以避免显示任何限制警告
                'limit': 999999,     # 实际上无限
                'tier_name': 'local'
            }
        
        try:
            result = await Cache.get(f"agent_count_limit:{account_id}")
            if result:
                logger.debug(f"Cache hit for agent count limit: {account_id}")
                return result
        except Exception as cache_error:
            logger.warning(f"Cache read failed for agent count limit {account_id}: {str(cache_error)}")

        # 提取 Agent 信息
        agents_result = await client.table('agents').select('agent_id, metadata').eq('user_id', account_id).execute()
        
        non_fufanmanus_agents = []
        for agent in agents_result.data or []:
            metadata = agent.get('metadata', {}) or {}
            is_fufanmanus_default = metadata.get('is_fufanmanus_default', False)
            if not is_fufanmanus_default:
                non_fufanmanus_agents.append(agent)
                
        current_count = len(non_fufanmanus_agents)
        logger.debug(f"Account {account_id} has {current_count} custom agents (excluding Fufanmanus defaults)")
        

        # TODO: 可以根据消费金额、用户授权等做Agent的创建限制
        # 这里不做限制，允许任意用户无限制访问
        result = {
            'can_create': True,
            'current_count': current_count,
            'limit': 999999,
            'tier_name': "free"
        }
        
        try:
            await Cache.set(f"agent_count_limit:{account_id}", result, ttl=300)
        except Exception as cache_error:
            logger.warning(f"Cache write failed for agent count limit {account_id}: {str(cache_error)}")
        
        logger.info(f"Account {account_id} has {current_count}/{999999} agents (tier: free) - can_create: {True}")
        
        return result
        
    except Exception as e:
        logger.error(f"Error checking agent count limit for account {account_id}: {str(e)}", exc_info=True)
        return {
            'can_create': True,
            'current_count': 0,
            'limit': 999999,
            'tier_name': 'free'
        }
