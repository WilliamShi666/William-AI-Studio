#!/usr/bin/env python3
"""
检查数据库中所有表的完整结构
"""

import asyncio
import asyncpg
from pathlib import Path

async def check_existing_tables():
    """检查现有表结构"""
    print("检查数据库中所有表的完整结构...")
    
    # 读取数据库连接
    database_url = None
    if Path('.env').exists():
        with open('.env', 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('DATABASE_URL='):
                    database_url = line.split('=', 1)[1].strip()
                    break
    
    if not database_url:
        print("未找到DATABASE_URL配置")
        return
    
    try:
        conn = await asyncpg.connect(database_url)
        
        # 1. 查看所有表
        tables = await conn.fetch("""
            SELECT table_name, table_type
            FROM information_schema.tables 
            WHERE table_schema = 'public'
            ORDER BY table_name
        """)
        
        print(f"\n数据库中所有表列表 ({len(tables)}个):")
        for table in tables:
            print(f"  - {table['table_name']} ({table['table_type']})")
        
        # 2. 显示每个表的详细结构
        for table in tables:
            table_name = table['table_name']
            print(f"\n{'='*60}")
            print(f"表 '{table_name}' 的详细结构:")
            print(f"{'='*60}")
            
            # 获取字段信息
            columns = await conn.fetch("""
                SELECT 
                    column_name, 
                    data_type, 
                    is_nullable,
                    column_default,
                    character_maximum_length,
                    numeric_precision,
                    numeric_scale
                FROM information_schema.columns 
                WHERE table_name = $1 
                AND table_schema = 'public'
                ORDER BY ordinal_position
            """, table_name)
            
            print(f"字段列表 ({len(columns)}个字段):")
            for col in columns:
                nullable = "NULL" if col['is_nullable'] == 'YES' else "NOT NULL"
                default = f" DEFAULT {col['column_default']}" if col['column_default'] else ""
                
                # 处理数据类型显示
                data_type = col['data_type']
                if col['character_maximum_length']:
                    data_type += f"({col['character_maximum_length']})"
                elif col['numeric_precision'] and col['numeric_scale'] is not None:
                    data_type += f"({col['numeric_precision']},{col['numeric_scale']})"
                elif col['numeric_precision']:
                    data_type += f"({col['numeric_precision']})"
                
                print(f"    {col['column_name']:<25}: {data_type:<20} {nullable:<8}{default}")
            
            # 获取主键信息
            primary_keys = await conn.fetch("""
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = $1::regclass AND i.indisprimary
            """, table_name)
            
            if primary_keys:
                pk_columns = [pk['attname'] for pk in primary_keys]
                print(f"主键: {', '.join(pk_columns)}")
            
            # 获取索引信息
            indexes = await conn.fetch("""
                SELECT 
                    indexname,
                    indexdef
                FROM pg_indexes 
                WHERE tablename = $1 
                AND schemaname = 'public'
                AND indexname NOT LIKE '%_pkey'
            """, table_name)
            
            if indexes:
                print(f"索引 ({len(indexes)}个):")
                for idx in indexes:
                    print(f"    - {idx['indexname']}")
                    print(f"      {idx['indexdef']}")
            
            # 获取外键信息
            foreign_keys = await conn.fetch("""
                SELECT
                    tc.constraint_name,
                    kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage AS ccu
                    ON ccu.constraint_name = tc.constraint_name
                    AND ccu.table_schema = tc.table_schema
                WHERE tc.constraint_type = 'FOREIGN KEY'
                    AND tc.table_name = $1
            """, table_name)
            
            if foreign_keys:
                print(f"外键 ({len(foreign_keys)}个):")
                for fk in foreign_keys:
                    print(f"    {fk['column_name']} -> {fk['foreign_table_name']}.{fk['foreign_column_name']}")
            
            # 获取表数据量
            try:
                row_count = await conn.fetchval(f"SELECT COUNT(*) FROM {table_name}")
                print(f"数据量: {row_count:,} 条记录")
                
                # 如果有数据，显示前几条记录的样例
                if row_count > 0 and row_count <= 10:
                    print(f"样例数据:")
                    sample_data = await conn.fetch(f"SELECT * FROM {table_name} LIMIT 3")
                    for i, row in enumerate(sample_data, 1):
                        print(f"  记录 {i}: {dict(row)}")
                elif row_count > 10:
                    print(f"表结构字段: {[col['column_name'] for col in columns[:5]]}{'...' if len(columns) > 5 else ''}")
                    
            except Exception as e:
                print(f"无法统计数据量: {e}")
        
        print(f"\n{'='*60}")
        print(f"表结构检查完成！共检查了 {len(tables)} 个表")
        print(f"{'='*60}")
        
        await conn.close()
        
    except Exception as e:
        print(f"检查表结构失败: {e}")

if __name__ == "__main__":
    asyncio.run(check_existing_tables()) 