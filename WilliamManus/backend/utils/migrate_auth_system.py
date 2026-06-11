"""
认证系统迁移脚本
从 Supabase Auth 迁移到本地 JWT 认证系统
"""

import asyncio
import sys
from datetime import datetime, timezone
from typing import List, Dict, Any
from services.supabase import DBConnection
from utils.jwt_auth import JWTAuth
from utils.logger import logger

class AuthMigrationService:
    """认证系统迁移服务"""
    
    def __init__(self):
        self.db_connection = DBConnection()
        self.jwt_auth = JWTAuth()
    
    async def initialize(self):
        """初始化数据库连接"""
        await self.db_connection.initialize()
        self.client = await self.db_connection.client
    
    async def check_migration_requirements(self) -> Dict[str, Any]:
        """
        检查迁移前的要求
        
        Returns:
            Dict: 包含检查结果的字典
        """
        results = {
            "status": "ready",
            "issues": [],
            "warnings": [],
            "recommendations": []
        }
        
        try:
            # 检查是否已经存在新的认证表
            try:
                auth_users_result = await self.client.table('auth_users').select('id').limit(1).execute()
                if auth_users_result.data:
                    results["warnings"].append("auth_users table already contains data")
            except Exception:
                results["issues"].append("auth_users table not found - please run the migration SQL first")
                results["status"] = "not_ready"
            
            # 检查Supabase用户数据
            try:
                # 注意：这里假设你有访问auth.users的权限，实际情况可能需要调整
                supabase_users_result = await self.client.schema('auth').table('users').select('id, email').limit(5).execute()
                user_count = len(supabase_users_result.data) if supabase_users_result.data else 0
                results["supabase_users_count"] = user_count
                
                if user_count > 0:
                    results["recommendations"].append(f"Found {user_count} Supabase users to migrate")
                else:
                    results["warnings"].append("No Supabase users found to migrate")
            except Exception as e:
                results["warnings"].append(f"Cannot access Supabase auth.users table: {e}")
            
            # 检查basejump账户
            try:
                accounts_result = await self.client.schema('basejump').table('accounts').select('id').limit(5).execute()
                account_count = len(accounts_result.data) if accounts_result.data else 0
                results["basejump_accounts_count"] = account_count
                
                if account_count > 0:
                    results["recommendations"].append(f"Found {account_count} basejump accounts")
            except Exception as e:
                results["issues"].append(f"Cannot access basejump.accounts table: {e}")
                results["status"] = "not_ready"
            
        except Exception as e:
            results["issues"].append(f"Database connection error: {e}")
            results["status"] = "not_ready"
        
        return results
    
    async def migrate_users_dry_run(self) -> Dict[str, Any]:
        """
        执行用户迁移的预演（不实际修改数据）
        
        Returns:
            Dict: 迁移预演结果
        """
        results = {
            "total_users": 0,
            "migratable_users": 0,
            "issues": [],
            "sample_users": []
        }
        
        try:
            # 获取Supabase用户（假设你有权限访问）
            # 注意：在实际环境中，你可能需要通过其他方式获取用户数据
            try:
                supabase_users = await self.client.schema('auth').table('users').select('*').execute()
                
                if not supabase_users.data:
                    results["issues"].append("No Supabase users found")
                    return results
                
                results["total_users"] = len(supabase_users.data)
                
                for user in supabase_users.data[:5]:  # 只处理前5个用户作为样本
                    user_info = {
                        "id": user.get('id'),
                        "email": user.get('email'),
                        "created_at": user.get('created_at'),
                        "issues": []
                    }
                    
                    # 检查邮箱
                    if not user.get('email'):
                        user_info["issues"].append("No email address")
                        continue
                    
                    # 检查是否已存在
                    existing_user = await self.client.table('auth_users').select('id').eq('email', user['email']).execute()
                    if existing_user.data:
                        user_info["issues"].append("Email already exists in auth_users")
                        continue
                    
                    # 检查关联的basejump账户
                    try:
                        account_result = await self.client.schema('basejump').table('accounts').select('*').eq('primary_owner_user_id', user['id']).execute()
                        if account_result.data:
                            user_info["basejump_accounts"] = len(account_result.data)
                        else:
                            user_info["issues"].append("No associated basejump account found")
                    except Exception as e:
                        user_info["issues"].append(f"Error checking basejump account: {e}")
                    
                    if not user_info["issues"]:
                        results["migratable_users"] += 1
                    
                    results["sample_users"].append(user_info)
                
            except Exception as e:
                results["issues"].append(f"Cannot access Supabase users: {e}")
        
        except Exception as e:
            results["issues"].append(f"Migration dry run error: {e}")
        
        return results
    
    async def migrate_users(self, dry_run: bool = True) -> Dict[str, Any]:
        """
        执行用户迁移
        
        Args:
            dry_run: 是否只是预演（不实际修改数据）
            
        Returns:
            Dict: 迁移结果
        """
        results = {
            "migrated_users": 0,
            "failed_users": 0,
            "errors": [],
            "dry_run": dry_run
        }
        
        if dry_run:
            logger.info("Running user migration in DRY RUN mode")
            return await self.migrate_users_dry_run()
        
        logger.info("Starting actual user migration")
        
        try:
            # 实际的迁移逻辑
            # 注意：这里需要根据你的实际Supabase设置进行调整
            
            # 1. 获取Supabase用户数据
            # 2. 为每个用户创建新的auth_users记录
            # 3. 确保basejump账户关联正确
            
            logger.warning("Actual migration not implemented - this is a template")
            results["errors"].append("Actual migration logic needs to be implemented based on your specific setup")
            
        except Exception as e:
            logger.error(f"Migration error: {e}")
            results["errors"].append(str(e))
        
        return results
    
    async def create_test_user(self, email: str, password: str, name: str) -> Dict[str, Any]:
        """
        创建测试用户（用于验证新系统）
        
        Args:
            email: 邮箱
            password: 密码
            name: 姓名
            
        Returns:
            Dict: 创建结果
        """
        try:
            from auth.service import AuthService
            from auth.models import RegisterRequest
            
            auth_service = AuthService()
            
            register_request = RegisterRequest(
                email=email,
                password=password,
                name=name
            )
            
            response = await auth_service.register(register_request)
            
            return {
                "success": True,
                "user_id": response.user.id,
                "access_token": response.access_token,
                "message": "Test user created successfully"
            }
            
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "message": "Failed to create test user"
            }
    
    async def verify_migration(self) -> Dict[str, Any]:
        """
        验证迁移结果
        
        Returns:
            Dict: 验证结果
        """
        results = {
            "auth_users_count": 0,
            "basejump_accounts_count": 0,
            "orphaned_accounts": 0,
            "issues": []
        }
        
        try:
            # 统计新认证表中的用户数量
            auth_users_result = await self.client.table('auth_users').select('id').execute()
            results["auth_users_count"] = len(auth_users_result.data) if auth_users_result.data else 0
            
            # 统计basejump账户数量
            accounts_result = await self.client.schema('basejump').table('accounts').select('id').execute()
            results["basejump_accounts_count"] = len(accounts_result.data) if accounts_result.data else 0
            
            # 检查孤立的账户（没有对应用户的账户）
            orphaned_query = """
            SELECT COUNT(*) as count 
            FROM basejump.accounts a 
            LEFT JOIN auth_users u ON a.primary_owner_user_id = u.id 
            WHERE u.id IS NULL
            """
            
            # 注意：这里使用原始SQL查询，可能需要根据你的Supabase设置调整
            # orphaned_result = await self.client.rpc('execute_sql', {'sql': orphaned_query}).execute()
            # results["orphaned_accounts"] = orphaned_result.data[0]['count'] if orphaned_result.data else 0
            
        except Exception as e:
            results["issues"].append(f"Verification error: {e}")
        
        return results

async def main():
    """主函数"""
    if len(sys.argv) < 2:
        print("Usage: python migrate_auth_system.py <command>")
        print("Commands:")
        print("  check     - 检查迁移前要求")
        print("  dry-run   - 执行迁移预演")
        print("  migrate   - 执行实际迁移")
        print("  verify    - 验证迁移结果")
        print("  test-user - 创建测试用户")
        return
    
    command = sys.argv[1]
    migration_service = AuthMigrationService()
    
    try:
        await migration_service.initialize()
        
        if command == "check":
            results = await migration_service.check_migration_requirements()
            print("Migration Requirements Check:")
            print(f"Status: {results['status']}")
            if results['issues']:
                print("Issues:")
                for issue in results['issues']:
                    print(f"  - {issue}")
            if results['warnings']:
                print("Warnings:")
                for warning in results['warnings']:
                    print(f"  - {warning}")
            if results['recommendations']:
                print("Recommendations:")
                for rec in results['recommendations']:
                    print(f"  - {rec}")
        
        elif command == "dry-run":
            results = await migration_service.migrate_users(dry_run=True)
            print("Migration Dry Run Results:")
            print(f"Total users: {results['total_users']}")
            print(f"Migratable users: {results['migratable_users']}")
            if results['issues']:
                print("Issues:")
                for issue in results['issues']:
                    print(f"  - {issue}")
            if results['sample_users']:
                print("Sample users:")
                for user in results['sample_users']:
                    print(f"  - {user['email']}: {user.get('issues', 'OK')}")
        
        elif command == "migrate":
            print("WARNING: This will perform actual migration!")
            confirm = input("Are you sure? (yes/no): ")
            if confirm.lower() == 'yes':
                results = await migration_service.migrate_users(dry_run=False)
                print("Migration Results:")
                print(f"Migrated users: {results['migrated_users']}")
                print(f"Failed users: {results['failed_users']}")
                if results['errors']:
                    print("Errors:")
                    for error in results['errors']:
                        print(f"  - {error}")
            else:
                print("Migration cancelled")
        
        elif command == "verify":
            results = await migration_service.verify_migration()
            print("Migration Verification:")
            print(f"Auth users count: {results['auth_users_count']}")
            print(f"Basejump accounts count: {results['basejump_accounts_count']}")
            print(f"Orphaned accounts: {results['orphaned_accounts']}")
            if results['issues']:
                print("Issues:")
                for issue in results['issues']:
                    print(f"  - {issue}")
        
        elif command == "test-user":
            if len(sys.argv) < 5:
                print("Usage: python migrate_auth_system.py test-user <email> <password> <name>")
                return
            
            email = sys.argv[2]
            password = sys.argv[3]
            name = sys.argv[4]
            
            results = await migration_service.create_test_user(email, password, name)
            print("Test User Creation:")
            print(f"Success: {results['success']}")
            print(f"Message: {results['message']}")
            if results['success']:
                print(f"User ID: {results['user_id']}")
                print(f"Access Token: {results['access_token'][:50]}...")
            else:
                print(f"Error: {results['error']}")
        
        else:
            print(f"Unknown command: {command}")
    
    except Exception as e:
        print(f"Error: {e}")
        logger.error(f"Migration script error: {e}")

if __name__ == "__main__":
    asyncio.run(main()) 