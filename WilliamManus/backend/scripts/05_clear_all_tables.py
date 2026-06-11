#!/usr/bin/env python3
"""
清空PostgreSQL所有表数据的脚本
Author: Muyu
"""

import asyncio
import sys
import os
from pathlib import Path

# 添加项目根目录到Python路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services.postgresql import DBConnection
from utils.logger import logger


async def clear_all_tables():
    """清空所有表的数据"""
    db = None
    try:
        # 初始化数据库连接
        db = DBConnection()
        await db.initialize()
        client = await db.client
        
        # 获取所有表名
        async with client.pool.acquire() as conn:
            tables = await conn.fetch(
                """
                SELECT tablename 
                FROM pg_tables 
                WHERE schemaname = 'public' 
                AND tablename NOT LIKE 'pg_%'
                ORDER BY tablename
                """
            )
        
        if not tables:
            logger.info("没有找到任何表")
            return
        
        logger.info(f"找到 {len(tables)} 个表:")
        for table in tables:
            logger.info(f"  - {table['tablename']}")
        
        # 确认操作
        print("\n警告：这将清空所有表的数据！")
        confirm = input("请输入 'YES' 确认继续，或按回车取消: ")
        
        if confirm != 'YES':
            logger.info("操作已取消")
            return
        
        # 禁用外键约束检查
        async with client.pool.acquire() as conn:
            await conn.execute("SET session_replication_role = replica;")
        
        # 清空所有表
        cleared_count = 0
        for table in tables:
            table_name = table['tablename']
            try:
                async with client.pool.acquire() as conn:
                    # 获取表的行数
                    count_result = await conn.fetchrow(
                        f"SELECT COUNT(*) as count FROM {table_name}"
                    )
                    row_count = count_result['count'] if count_result else 0
                    
                    # 清空表
                    await conn.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE;")
                
                logger.info(f"已清空表 {table_name} ({row_count} 行数据)")
                cleared_count += 1
                
            except Exception as e:
                logger.error(f"清空表 {table_name} 失败: {e}")
        
        # 重新启用外键约束检查
        async with client.pool.acquire() as conn:
            await conn.execute("SET session_replication_role = DEFAULT;")
        
        logger.info(f"\n操作完成！成功清空了 {cleared_count} 个表的数据")
        
        # 验证清空结果
        print("\n清空后的表状态:")
        for table in tables:
            table_name = table['tablename']
            try:
                async with client.pool.acquire() as conn:
                    count_result = await conn.fetchrow(
                        f"SELECT COUNT(*) as count FROM {table_name}"
                    )
                    row_count = count_result['count'] if count_result else 0
                    status = "已清空" if row_count == 0 else f"仍有 {row_count} 行数据"
                    print(f"  {table_name}: {status}")
            except Exception as e:
                print(f"  {table_name}: 检查失败 - {e}")
        
    except Exception as e:
        logger.error(f"清空表数据时发生错误: {e}")
        raise
    finally:
        # 关闭数据库连接
        if db:
            await DBConnection.disconnect()


async def clear_specific_tables(table_names: list):
    """清空指定表的数据"""
    db = None
    try:
        # 初始化数据库连接
        db = DBConnection()
        await db.initialize()
        client = await db.client
        
        logger.info(f"准备清空以下表: {', '.join(table_names)}")
        
        # 确认操作
        print(f"\n警告：这将清空表 {', '.join(table_names)} 的数据！")
        confirm = input("请输入 'YES' 确认继续，或按回车取消: ")
        
        if confirm != 'YES':
            logger.info("操作已取消")
            return
        
        # 禁用外键约束检查
        async with client.pool.acquire() as conn:
            await conn.execute("SET session_replication_role = replica;")
        
        cleared_count = 0
        for table_name in table_names:
            try:
                async with client.pool.acquire() as conn:
                    # 检查表是否存在
                    exists = await conn.fetchrow(
                        "SELECT 1 FROM pg_tables WHERE tablename = $1 AND schemaname = 'public'",
                        table_name
                    )
                    
                    if not exists:
                        logger.warning(f"表 {table_name} 不存在，跳过")
                        continue
                    
                    # 获取表的行数
                    count_result = await conn.fetchrow(
                        f"SELECT COUNT(*) as count FROM {table_name}"
                    )
                    row_count = count_result['count'] if count_result else 0
                    
                    # 清空表
                    await conn.execute(f"TRUNCATE TABLE {table_name} RESTART IDENTITY CASCADE;")
                
                logger.info(f"已清空表 {table_name} ({row_count} 行数据)")
                cleared_count += 1
                
            except Exception as e:
                logger.error(f"清空表 {table_name} 失败: {e}")
        
        # 重新启用外键约束检查
        async with client.pool.acquire() as conn:
            await conn.execute("SET session_replication_role = DEFAULT;")
        
        logger.info(f"\n操作完成！成功清空了 {cleared_count} 个表的数据")
        
    except Exception as e:
        logger.error(f"清空表数据时发生错误: {e}")
        raise
    finally:
        if db:
            await DBConnection.disconnect()


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="清空PostgreSQL表数据")
    parser.add_argument(
        "--tables", 
        nargs="+", 
        help="指定要清空的表名（不指定则清空所有表）"
    )
    parser.add_argument(
        "--force", 
        action="store_true", 
        help="强制清空，跳过确认步骤"
    )
    
    args = parser.parse_args()
    
    if args.tables:
        # 清空指定表
        asyncio.run(clear_specific_tables(args.tables))
    else:
        # 清空所有表
        asyncio.run(clear_all_tables())


if __name__ == "__main__":
    main() 