import { NextRequest, NextResponse } from 'next/server';
import { Pool } from 'pg';

// 创建PostgreSQL连接池
const pool = new Pool({
  host: process.env.DB_HOST || 'localhost',
  port: parseInt(process.env.DB_PORT || '5432'),
  database: process.env.DB_NAME || 'kortix',
  user: process.env.DB_USER || 'postgres',
  password: process.env.DB_PASSWORD || 'password',
  max: 20, // 最大连接数
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 2000,
});

export async function POST(request: NextRequest) {
  try {
    const { sql, params } = await request.json();
    
    if (!sql) {
      return NextResponse.json(
        { error: 'SQL query is required' },
        { status: 400 }
      );
    }

    // 执行SQL查询
    const client = await pool.connect();
    try {
      const result = await client.query(sql, params);
      return NextResponse.json({
        rows: result.rows,
        rowCount: result.rowCount,
        command: result.command
      });
    } finally {
      client.release();
    }
  } catch (error: any) {
    console.error('Database query error:', error);
    return NextResponse.json(
      { 
        error: 'Database query failed', 
        details: error.message,
        sql: request.body // 调试信息
      },
      { status: 500 }
    );
  }
}

// 健康检查端点
export async function GET() {
  try {
    const client = await pool.connect();
    try {
      await client.query('SELECT 1');
      return NextResponse.json({ status: 'healthy', timestamp: new Date().toISOString() });
    } finally {
      client.release();
    }
  } catch (error: any) {
    return NextResponse.json(
      { status: 'unhealthy', error: error.message },
      { status: 500 }
    );
  }
} 