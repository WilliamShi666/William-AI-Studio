import datetime
from typing import Dict, Any
from dataclasses import dataclass
from agent.fufanmanus.config import FufanmanusConfig


@dataclass
class FufanmanusConfiguration:
    name: str
    description: str
    configured_mcps: list
    custom_mcps: list
    restrictions: Dict[str, Any]
    version_tag: str


class FufanmanusConfigManager:
    def get_current_config(self) -> FufanmanusConfiguration:
        version_tag = self._generate_version_tag()
        
        return FufanmanusConfiguration(
            name=FufanmanusConfig.NAME,
            description=FufanmanusConfig.DESCRIPTION,
            configured_mcps=FufanmanusConfig.DEFAULT_MCPS.copy(),
            custom_mcps=FufanmanusConfig.DEFAULT_CUSTOM_MCPS.copy(),
            restrictions=FufanmanusConfig.USER_RESTRICTIONS.copy(),
            version_tag=version_tag
        )
    
    def has_config_changed(self, last_version_tag: str) -> bool:
        current = self.get_current_config()
        return current.version_tag != last_version_tag
    
    def validate_config(self, config: FufanmanusConfiguration) -> tuple[bool, list[str]]:
        errors = []
        
        if not config.name.strip():
            errors.append("Name cannot be empty")
            
        return len(errors) == 0, errors
    
    def _generate_version_tag(self) -> str:
        import hashlib
        import json
        
        config_data = {
            "name": FufanmanusConfig.NAME,
            "description": FufanmanusConfig.DESCRIPTION,
            "system_prompt": FufanmanusConfig.get_system_prompt(),
            "default_tools": FufanmanusConfig.DEFAULT_TOOLS,
            "avatar": FufanmanusConfig.AVATAR,
            "avatar_color": FufanmanusConfig.AVATAR_COLOR,
            "restrictions": FufanmanusConfig.USER_RESTRICTIONS,
        }
        
        config_str = json.dumps(config_data, sort_keys=True)
        hash_obj = hashlib.md5(config_str.encode())
        return f"config-{hash_obj.hexdigest()[:8]}" 