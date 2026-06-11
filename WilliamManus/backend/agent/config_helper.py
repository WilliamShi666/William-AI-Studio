from typing import Dict, Any, Optional, List
from utils.logger import logger


def extract_agent_config(agent_data: Dict[str, Any], version_data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    agent_id = agent_data.get('agent_id', 'Unknown')

    # 处理metadata字段，确保它是字典
    metadata_raw = agent_data.get('metadata', {})
    if isinstance(metadata_raw, str):
        try:
            import json
            metadata = json.loads(metadata_raw)
        except (json.JSONDecodeError, TypeError):
            metadata = {}
    else:
        metadata = metadata_raw if isinstance(metadata_raw, dict) else {}
    
    is_fufanmanus_default = metadata.get('is_fufanmanus_default', False)
    centrally_managed = metadata.get('centrally_managed', False)
    restrictions = metadata.get('restrictions', {})
    
    if version_data:
        logger.info(f"Using active version data for agent {agent_id} (version: {version_data.get('version_name', 'unknown')})")
        
        model = None
        workflows = []
        if version_data.get('config'):
            config = version_data['config'].copy()
            system_prompt = config.get('system_prompt', '')
            model = config.get('model')
            tools = config.get('tools', {})
            configured_mcps = tools.get('mcp', [])
            custom_mcps = tools.get('custom_mcp', [])
            agentpress_tools = tools.get('agentpress', {})
            workflows = config.get('workflows', [])
        else:
            system_prompt = version_data.get('system_prompt', '')
            model = version_data.get('model')
            configured_mcps = version_data.get('configured_mcps', [])
            custom_mcps = version_data.get('custom_mcps', [])
            agentpress_tools = version_data.get('agentpress_tools', {})
            workflows = []
        
        if is_fufanmanus_default:
            from agent.fufanmanus.config import FufanmanusConfig
            system_prompt = FufanmanusConfig.get_system_prompt()
            agentpress_tools = FufanmanusConfig.DEFAULT_TOOLS
        
        config = {
            'agent_id': agent_data['agent_id'],
            'name': agent_data['name'],
            'description': agent_data.get('description'),
            'is_default': agent_data.get('is_default', False),
            'account_id': agent_data.get('account_id') or agent_data.get('user_id'),
            'current_version_id': agent_data.get('current_version_id'),
            'version_name': version_data.get('version_name', 'v1'),
            'system_prompt': system_prompt,
            'model': model,
            'configured_mcps': configured_mcps,
            'custom_mcps': custom_mcps,
            'agentpress_tools': _extract_agentpress_tools_for_run(agentpress_tools),
            'workflows': workflows,
            'avatar': agent_data.get('avatar'),
            'avatar_color': agent_data.get('avatar_color'),
            'profile_image_url': agent_data.get('profile_image_url'),
            'is_fufanmanus_default': is_fufanmanus_default,
            'centrally_managed': centrally_managed,
            'restrictions': restrictions
        }
        
        return config
    
    # 检查是否有直接的配置字段（agents表的设计）
    if agent_data.get('system_prompt') or agent_data.get('model'):
        logger.info(f"Using direct agent fields for agent {agent_id}")
        
        # 处理JSON字段
        configured_mcps_raw = agent_data.get('configured_mcps', [])
        if isinstance(configured_mcps_raw, str):
            try:
                import json
                configured_mcps = json.loads(configured_mcps_raw)
            except (json.JSONDecodeError, TypeError):
                configured_mcps = []
        else:
            configured_mcps = configured_mcps_raw if isinstance(configured_mcps_raw, list) else []
        
        custom_mcps_raw = agent_data.get('custom_mcps', [])
        if isinstance(custom_mcps_raw, str):
            try:
                import json
                custom_mcps = json.loads(custom_mcps_raw)
            except (json.JSONDecodeError, TypeError):
                custom_mcps = []
        else:
            custom_mcps = custom_mcps_raw if isinstance(custom_mcps_raw, list) else []
        
        agentpress_tools_raw = agent_data.get('agentpress_tools', {})
        if isinstance(agentpress_tools_raw, str):
            try:
                import json
                agentpress_tools = json.loads(agentpress_tools_raw)
            except (json.JSONDecodeError, TypeError):
                agentpress_tools = {}
        else:
            agentpress_tools = agentpress_tools_raw if isinstance(agentpress_tools_raw, dict) else {}
        
        if is_fufanmanus_default:
            from agent.fufanmanus.config import FufanmanusConfig
            system_prompt = FufanmanusConfig.get_system_prompt()
            agentpress_tools = FufanmanusConfig.DEFAULT_TOOLS
        else:
            system_prompt = agent_data.get('system_prompt', '')
        
        config = {
            'agent_id': agent_data['agent_id'],
            'name': agent_data['name'],
            'description': agent_data.get('description'),
            'is_default': agent_data.get('is_default', False),
            'account_id': agent_data.get('account_id') or agent_data.get('user_id'),
            'current_version_id': agent_data.get('current_version_id'),
            'version_name': 'v1',
            'system_prompt': system_prompt,
            'model': agent_data.get('model'),
            'configured_mcps': configured_mcps,
            'custom_mcps': custom_mcps,
            'agentpress_tools': _extract_agentpress_tools_for_run(agentpress_tools),
            'workflows': [],
            'avatar': agent_data.get('avatar'),
            'avatar_color': agent_data.get('avatar_color'),
            'profile_image_url': agent_data.get('profile_image_url'),
            'is_fufanmanus_default': is_fufanmanus_default,
            'centrally_managed': centrally_managed,
            'restrictions': restrictions
        }
        
        return config
    
    if agent_data.get('config'):
        logger.info(f"Using agent config for agent {agent_id}")
        config = agent_data['config'].copy()
        
        if is_fufanmanus_default:
            from agent.fufanmanus.config import FufanmanusConfig
            config['system_prompt'] = FufanmanusConfig.get_system_prompt()
            config['tools']['agentpress'] = FufanmanusConfig.DEFAULT_TOOLS
        
        config.update({
            'agent_id': agent_data['agent_id'],
            'name': agent_data['name'],
            'description': agent_data.get('description'),
            'is_default': agent_data.get('is_default', False),
            'account_id': agent_data.get('account_id') or agent_data.get('user_id'),
            'current_version_id': agent_data.get('current_version_id'),
            'model': config.get('model'),  # Include model from config
            'is_fufanmanus_default': is_fufanmanus_default,
            'centrally_managed': centrally_managed,
            'restrictions': restrictions
        })
        
        tools = config.get('tools', {})
        config['configured_mcps'] = tools.get('mcp', [])
        config['custom_mcps'] = tools.get('custom_mcp', [])
        config['agentpress_tools'] = _extract_agentpress_tools_for_run(tools.get('agentpress', {}))
        config['workflows'] = config.get('workflows', [])
        
        # Legacy and new fields
        config['avatar'] = agent_data.get('avatar')
        config['avatar_color'] = agent_data.get('avatar_color')
        config['profile_image_url'] = agent_data.get('profile_image_url')
        
        return config
    
    # Fallback: Create default configuration for agents without version or config data
    logger.warning(f"No config found for agent {agent_id}, creating default configuration")
    
    # Create minimal default configuration
    config = {
        'agent_id': agent_data['agent_id'],
        'name': agent_data.get('name', 'Unnamed Agent'),
        'description': agent_data.get('description', ''),
        'is_default': agent_data.get('is_default', False),
        'account_id': agent_data.get('account_id') or agent_data.get('user_id'),
        'current_version_id': agent_data.get('current_version_id'),
        'version_name': 'v1',
        'system_prompt': 'You are a helpful AI assistant.',
        'model': None,  # No model specified for default config
        'configured_mcps': [],
        'custom_mcps': [],
        'agentpress_tools': {},
        'workflows': [],
        'avatar': agent_data.get('avatar'),
        'avatar_color': agent_data.get('avatar_color'),
        'profile_image_url': agent_data.get('profile_image_url'),
        'is_fufanmanus_default': is_fufanmanus_default,
        'centrally_managed': centrally_managed,
        'restrictions': restrictions
    }
    
    return config


def build_unified_config(
    system_prompt: str,
    agentpress_tools: Dict[str, Any],
    configured_mcps: List[Dict[str, Any]],
    custom_mcps: Optional[List[Dict[str, Any]]] = None,
    avatar: Optional[str] = None,
    avatar_color: Optional[str] = None,
    fufanmanus_metadata: Optional[Dict[str, Any]] = None,
    workflows: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    simplified_tools = {}
    for tool_name, tool_config in agentpress_tools.items():
        if isinstance(tool_config, dict):
            simplified_tools[tool_name] = tool_config.get('enabled', False)
        elif isinstance(tool_config, bool):
            simplified_tools[tool_name] = tool_config
    
    config = {
        'system_prompt': system_prompt,
        'tools': {
            'agentpress': simplified_tools,
            'mcp': configured_mcps or [],
            'custom_mcp': custom_mcps or []
        },
        'workflows': workflows or [],
        'metadata': {
            'avatar': avatar,
            'avatar_color': avatar_color
        }
    }
    
    if fufanmanus_metadata:
        config['fufanmanus_metadata'] = fufanmanus_metadata
    
    return config


def _extract_agentpress_tools_for_run(agentpress_config: Dict[str, Any]) -> Dict[str, Any]:
    if not agentpress_config:
        return {}
    
    run_tools = {}
    for tool_name, enabled in agentpress_config.items():
        if isinstance(enabled, bool):
            run_tools[tool_name] = {
                'enabled': enabled,
                'description': f"{tool_name} tool"
            }
        elif isinstance(enabled, dict):
            run_tools[tool_name] = enabled
        else:
            run_tools[tool_name] = {
                'enabled': bool(enabled),
                'description': f"{tool_name} tool"
            }
    
    return run_tools


def extract_tools_for_agent_run(config: Dict[str, Any]) -> Dict[str, Any]:
    logger.warning("extract_tools_for_agent_run is deprecated, using config-based extraction")
    tools = config.get('tools', {})
    return _extract_agentpress_tools_for_run(tools.get('agentpress', {}))


def get_mcp_configs(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    tools = config.get('tools', {})
    all_mcps = []
    
    if 'configured_mcps' in config and config['configured_mcps']:
        for mcp in config['configured_mcps']:
            if mcp not in all_mcps:
                all_mcps.append(mcp)
    
    if 'custom_mcps' in config and config['custom_mcps']:
        for mcp in config['custom_mcps']:
            if mcp not in all_mcps:
                all_mcps.append(mcp)
    
    mcp_list = tools.get('mcp', [])
    if mcp_list:
        for mcp in mcp_list:
            if mcp not in all_mcps:
                all_mcps.append(mcp)
    
    custom_mcp_list = tools.get('custom_mcp', [])
    if custom_mcp_list:
        for mcp in custom_mcp_list:
            if mcp not in all_mcps:
                all_mcps.append(mcp)
    
    return all_mcps


def is_fufanmanus_default_agent(config: Dict[str, Any]) -> bool:
    return config.get('is_fufanmanus_default', False)


def get_agent_restrictions(config: Dict[str, Any]) -> Dict[str, bool]:
    return config.get('restrictions', {})


def can_edit_field(config: Dict[str, Any], field_name: str) -> bool:
    if not is_fufanmanus_default_agent(config):
        return True
    
    restrictions = get_agent_restrictions(config)
    return restrictions.get(field_name, True)


def get_default_system_prompt_for_fufanmanus_agent() -> str:
    from agent.fufanmanus.config import FufanmanusConfig
    return FufanmanusConfig.get_system_prompt()


