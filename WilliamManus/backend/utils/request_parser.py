"""
Request Parser Tool, parse request data from different formats
"""

import json
from typing import Dict, Any, Optional
from fastapi import Request  # type: ignore
from utils.logger import logger

async def parse_request_data(request: Request) -> Dict[str, Any]:
    """
    Parse request data, support multiple formats
    
    Args:
        request: FastAPI request object
    
    Returns:
        Parsed data dictionary
    """
    result = {}
    
    # 获取原始数据
    body = await request.body()
    
    # 尝试解析为表单数据
    try:
        form_data = await request.form()
        result.update(dict(form_data))
    except Exception as e:
        logger.debug(f"Form data parsing failed: {e}")
    
    # 尝试解析为JSON数据
    try:
        json_data = await request.json()
        result.update(json_data)
    except Exception as e:
        logger.debug(f"JSON data parsing failed: {e}")
    
    # 尝试解析为查询参数
    try:
        query_params = dict(request.query_params)
        result.update(query_params)
    except Exception as e:
        logger.debug(f"Query params parsing failed: {e}")
    
    # 尝试解析为URL编码数据
    try:
        if body:
            body_str = body.decode('utf-8')
            
            # 解析URL编码格式
            if '=' in body_str and '&' in body_str:
                url_encoded = {}
                for pair in body_str.split('&'):
                    if '=' in pair:
                        key, value = pair.split('=', 1)
                        url_encoded[key] = value
                result.update(url_encoded)
            
            # 解析多行格式（如前端发送的格式）
            elif '\n' in body_str:
                lines = body_str.strip().split('\n')
                multi_line = {}
                for i in range(0, len(lines), 2):
                    if i + 1 < len(lines):
                        key = lines[i].strip()
                        value = lines[i + 1].strip()
                        multi_line[key] = value
                result.update(multi_line)
    
    except Exception as e:
        logger.debug(f"Body parsing failed: {e}")
    
    # 只打印最终的解析结果
    logger.info(f"Request data parsed: {result}")
    return result

def extract_auth_data(data: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """
    Extract authentication information from parsed data
    
    Args:
        data: Parsed request data dictionary
    
    Returns:
        Dictionary containing authentication information
    """
    email = None
    password = None
    confirmPassword = None
    origin = None
    
    # 尝试多种可能的键名（兼容不同的前端格式）
    email_keys = ['email', '1_email', 'user_email', 'mail']
    password_keys = ['password', '1_password', 'user_password', 'pass']
    confirm_keys = ['confirmPassword', '1_confirmPassword', 'confirm_password', 'password_confirm']
    origin_keys = ['origin', '1_origin', 'referer', 'source']
    
    # 提取邮箱
    for key in email_keys:
        if key in data:
            email = data[key]
            break
    
    # 提取密码
    for key in password_keys:
        if key in data:
            password = data[key]
            break
    
    # 提取确认密码
    for key in confirm_keys:
        if key in data:
            confirmPassword = data[key]
            break
    
    # 提取来源信息
    for key in origin_keys:
        if key in data:
            origin = data[key]
            break
    
    auth_data = {
        'email': email,
        'password': password,
        'confirmPassword': confirmPassword,
        'origin': origin
    }
    
    logger.debug(f"Extracted auth data: {auth_data}")
    return auth_data 