#!/usr/bin/env python3
"""
Redis setup script
Help quickly configure Redis connection and test connectivity
"""

import asyncio
import os
import sys
import re
from pathlib import Path

async def test_redis_connection():
    """Test Redis connection"""
    print("Test Redis connection...")
    
    try:
        import redis.asyncio as redis # type: ignore
        print("redis is installed")
    except ImportError:
        print("redis is not installed, please run: pip install redis")
        return False
    
    # Get Redis connection information
    print("\nPlease enter Redis connection information:")
    host = input("Redis host (default: localhost): ").strip() or "localhost"
    port = input("Redis port (default: 6379): ").strip() or "6379"
    password = input("Redis password (press Enter if no password): ").strip()
    
    print(f"\nConnection info: redis://{host}:{port}")
    
    try:
        # Test Redis connection
        if password:
            redis_client = redis.Redis(host=host, port=int(port), password=password, decode_responses=True)
        else:
            redis_client = redis.Redis(host=host, port=int(port), decode_responses=True)
        
        # Test ping
        await redis_client.ping()
        print("Redis connection successful")
        
        # Test basic operations
        await redis_client.set("test_key", "test_value")
        value = await redis_client.get("test_key")
        await redis_client.delete("test_key")
        
        if value == "test_value":
            print("Redis read/write operations successful")
        
        await redis_client.aclose()
        
        # Update .env file
        update_redis_config_in_env(host, port, password)
        print("Configuration saved to .env file")
        return True
        
    except Exception as e:
        print(f"Redis connection failed: {e}")
        print("\nCommon solutions:")
        print("1. Please start Redis server first")
        print("2. Check if Redis is running on the specified host and port")
        print("3. Check if password is correct")
        print("4. Check firewall settings")
        return False

def update_redis_config_in_env(host, port, password):
    """Update Redis configuration in .env file"""
    env_file = '.env'
    
    # Redis configuration lines
    redis_host_line = f"REDIS_HOST={host}"
    redis_port_line = f"REDIS_PORT={port}"
    redis_password_line = f"REDIS_PASSWORD={password}" if password else "REDIS_PASSWORD="
    
    if os.path.exists(env_file):
        # Read existing .env file
        with open(env_file, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Add Redis configuration comment if it doesn't exist
        if '# Redis configuration' not in content:
            content += f"\n# Redis configuration"
        
        # Update existing Redis configuration
        content = re.sub(r'REDIS_HOST=.*', redis_host_line, content)
        content = re.sub(r'REDIS_PORT=.*', redis_port_line, content)
        content = re.sub(r'REDIS_PASSWORD=.*', redis_password_line, content)
        
        # Add Redis configuration if not exists
        if 'REDIS_HOST=' not in content:
            content += f"\n{redis_host_line}"
        if 'REDIS_PORT=' not in content:
            content += f"\n{redis_port_line}"
        if 'REDIS_PASSWORD=' not in content:
            content += f"\n{redis_password_line}"
        
        with open(env_file, 'w', encoding='utf-8') as f:
            f.write(content)
    else:
        # Create new .env file with Redis configuration
        redis_content = f"""# Redis configuration
{redis_host_line}
{redis_port_line}
{redis_password_line}
"""
        with open(env_file, 'w', encoding='utf-8') as f:
            f.write(redis_content)

async def main():
    """Main function"""
    print("Redis setup guide")
    print("=" * 40)
    
    # Test Redis connection
    if not await test_redis_connection():
        print("\nRedis connection failed, please check the configuration and try again")
        return
    
    print("\nRedis setup completed!")
    print("\nNext you can:")
    print("1. Start your application with Redis support")
    print("2. Check the Redis configuration in the .env file")

if __name__ == "__main__":
    asyncio.run(main()) 