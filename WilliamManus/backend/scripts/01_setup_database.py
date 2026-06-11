#!/usr/bin/env python3
"""
Database setup script
Help quickly configure PostgreSQL database connection and table structure
"""

import asyncio
import os
import secrets
import sys
from pathlib import Path

async def create_database_if_not_exists(host, port, username, password, database):
    """Create database if it does not exist"""
    try:
        import asyncpg # type: ignore
        
        # Connect to postgres default database
        postgres_url = f"postgresql://{username}:{password}@{host}:{port}/postgres"
        conn = await asyncpg.connect(postgres_url)
        
        # Check if target database exists
        result = await conn.fetchrow(
            "SELECT 1 FROM pg_database WHERE datname = $1", database
        )
        
        if result:
            print(f"Database '{database}' already exists")
        else:
            print(f"Database '{database}' does not exist, creating...")
            # Create database
            await conn.execute(f'CREATE DATABASE "{database}"')
            print(f"Database '{database}' created successfully")
        
        await conn.close()
        return True
        
    except Exception as e:
        print(f"Database creation failed: {e}")
        return False

async def test_database_connection():
    """测试数据库连接"""
    print("Test database connection...")
    
    try:
        import asyncpg # type: ignore
        print("asyncpg is installed")
    except ImportError:
        print("asyncpg is not installed, please run: pip install asyncpg")
        return False
    
    # 获取数据库连接信息
    print("\nPlease enter database connection information:")
    host = input("Host address (default: localhost): ").strip() or "localhost"
    port = input("Port (default: 5432): ").strip() or "5432"
    database = input("Database name (default: fufanmanus): ").strip() or "fufanmanus"
    username = input("Username (default: postgres): ").strip() or "postgres"
    password = input("Password: ").strip()
    
    if not password:
        print("Password cannot be empty")
        return False
    
    # 构建连接字符串
    database_url = f"postgresql://{username}:{password}@{host}:{port}/{database}"
    print(f"\nConnection string: postgresql://{username}:***@{host}:{port}/{database}")
    
    try:
        # 测试连接
        conn = await asyncpg.connect(database_url)
        print("Database connection successful")
        
        # 测试查询
        result = await conn.fetchval("SELECT version()")
        print(f"PostgreSQL version: {result.split(',')[0]}")
        
        await conn.close()
        
        # 保存配置到.env文件
        # JWT:（JSON Web Token）是一种开放标准（RFC 7519），用于在不同系统之间安全地传递信息
        # 让服务器和客户端之间安全地传递身份验证和授权信息，常用于登录态管理、API 授权、分布式系统单点登录等场景
        env_content = f"""# Database configuration
DATABASE_URL={database_url}

# JWT configuration
JWT_SECRET_KEY={secrets.token_urlsafe(48)}
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
REFRESH_TOKEN_EXPIRE_DAYS=30
"""
        
        with open('.env', 'w', encoding='utf-8') as f:
            f.write(env_content)
        
        print("Configuration saved to .env file")
        return True
        
    except Exception as e:
        print(f"Database connection failed: {e}")
        
        # Try to create database if it doesn't exist
        if "does not exist" in str(e):
            print("Attempting to create database...")
            if await create_database_if_not_exists(host, port, username, password, database):
                print("Retrying connection...")
                try:
                    # Retry connection after creating database
                    conn = await asyncpg.connect(database_url)
                    print("Database connection successful")
                    
                    # Test query
                    result = await conn.fetchval("SELECT version()")
                    print(f"PostgreSQL version: {result.split(',')[0]}")
                    
                    await conn.close()
                    
                    # Save configuration to .env file
                    env_content = f"""# Database configuration
DATABASE_URL={database_url}

# JWT configuration
JWT_SECRET_KEY={secrets.token_urlsafe(48)}
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440
REFRESH_TOKEN_EXPIRE_DAYS=30
"""
                    
                    with open('.env', 'w', encoding='utf-8') as f:
                        f.write(env_content)
                    
                    print("Configuration saved to .env file")
                    return True
                    
                except Exception as retry_e:
                    print(f"Connection still failed after database creation: {retry_e}")
        
        print("\nCommon solutions:")
        print("1. Check if PostgreSQL service is running")
        print("2. Check if username and password are correct")
        print("3. Check if user has permission to create databases")
        print("4. Check firewall settings")
        return False

async def main():
    """Main function"""
    print("Database setup guide")
    print("=" * 50)
    
    # 步骤1: 测试数据库连接
    if not await test_database_connection():
        print("\nDatabase connection failed, please check the configuration and try again")
        return
    
    print("\nDatabase setup completed!")
    print("\nNext you can:")
    print("1. Start FastAPI server: python -m uvicorn api:app --reload")
    print("2. Test API endpoints: POST /api/auth/register")
    print("3. Check the configuration in the .env file")

if __name__ == "__main__":
    asyncio.run(main())
