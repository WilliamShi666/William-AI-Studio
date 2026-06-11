"""
AgentPress PostgreSQL Database Connection Manager


DBConnection (单例管理器) ：整个系统的数据库访问入口
    ↓
PostgreSQLClient (客户端包装器) ： 是查询构建器的工厂
    ↓
PostgreSQLSchema / PostgreSQLTable (查询构建器)：99%的数据库操作都通过它
    ↓
QueryResult (结果包装器) ：统一查询结果格式

"""

import asyncio
from typing import Optional, List, Dict, Any, Union
import asyncpg # type: ignore
from utils.logger import logger
from utils.config import config
import threading
import os
import json


def _read_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid integer for %s=%r, using default=%s",
            name,
            raw_value,
            default,
        )
        return default

class DBConnection:
    """Thread-safe singleton database connection manager using PostgreSQL"""
    
    _instance: Optional['DBConnection'] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                # Double-checked locking pattern for thread safety
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
                    cls._instance._pool = None
                    cls._instance._initialize_lock = threading.Lock()
                    cls._instance._initializing = False
                    cls._instance._initialize_event = None
                    cls._instance._initialize_error_message = None
        return cls._instance

    def __init__(self):
        """Initialization method, actual initialization is not performed here"""
        pass

    async def initialize(self):
        """Initialize database connection pool"""
        if self._initialized:
            return

        wait_event = None
        should_initialize = False
        with self._initialize_lock:
            if self._initialized:
                return
            if self._initializing:
                wait_event = self._initialize_event
            else:
                self._initializing = True
                self._initialize_error_message = None
                self._initialize_event = threading.Event()
                wait_event = self._initialize_event
                should_initialize = True

        if not should_initialize:
            if wait_event is not None:
                await asyncio.to_thread(wait_event.wait)
            if self._initialized:
                return
            error_message = self._initialize_error_message or "unknown initialization error"
            raise RuntimeError(error_message)

        try:
            # Get database URL from environment variables or config file
            database_url = os.getenv('DATABASE_URL')
            if not database_url:
                if hasattr(config, 'DATABASE_URL') and config.DATABASE_URL:
                    database_url = config.DATABASE_URL
                else:
                    # Default connection string for development environment
                    database_url = "postgresql://postgres:password@localhost:5432/fufanmanus"

            if not database_url:
                logger.error("Missing PostgreSQL DATABASE_URL environment variable")
                raise RuntimeError("PostgreSQL DATABASE_URL environment variable must be set.")

            pool = await asyncpg.create_pool(
                database_url,
                min_size=_read_int_env("PG_POOL_MIN_SIZE", 2), # Minimum number of connections
                max_size=_read_int_env("PG_POOL_MAX_SIZE", 30), # Maximum number of connections
                command_timeout=_read_int_env("PG_COMMAND_TIMEOUT", 90) # Command timeout
            )
        except BaseException as e:
            error_message = f"PostgreSQL connection pool initialization failed: {str(e)}"
            with self._initialize_lock:
                self._pool = None
                self._initialized = False
                self._initializing = False
                self._initialize_error_message = error_message
                if self._initialize_event is not None:
                    self._initialize_event.set()
            if isinstance(e, asyncio.CancelledError):
                logger.warning("PostgreSQL connection pool initialization cancelled")
                raise
            logger.error(f"PostgreSQL connection pool initialization error: {e}")
            raise RuntimeError(error_message) from e

        with self._initialize_lock:
            self._pool = pool
            self._initialized = True
            self._initializing = False
            self._initialize_error_message = None
            if self._initialize_event is not None:
                self._initialize_event.set()
        logger.info("PostgreSQL connection pool initialized successfully")

    @property
    async def client(self):
        """Get database client from connection pool"""
        if not self._initialized:
            await self.initialize()
        return PostgreSQLClient(self._pool)

    @classmethod
    async def disconnect(cls):
        """Disconnect database connection"""
        if not cls._instance:
            return

        pool = None
        with cls._instance._initialize_lock:
            pool = cls._instance._pool
            cls._instance._pool = None
            cls._instance._initialized = False
            cls._instance._initializing = False
            cls._instance._initialize_error_message = None
            if cls._instance._initialize_event is not None:
                cls._instance._initialize_event.set()
            cls._instance._initialize_event = None

        if pool:
            await pool.close()
            logger.info("PostgreSQL connection pool closed")

class PostgreSQLClient:
    """PostgreSQL client wrapper providing database operation interfaces"""
    
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool
    
    def table(self, table_name: str):
        """Create table query builder"""
        return PostgreSQLTable(self.pool, table_name)
    
    def schema(self, schema_name: str):
        """Create schema query builder (for Supabase schema compatibility)"""
        return PostgreSQLSchema(self.pool, schema_name)

class PostgreSQLSchema:
    """Schema query builder for supporting schema functionality"""
    
    def __init__(self, pool: asyncpg.Pool, schema_name: str):
        self.pool = pool
        self.schema_name = schema_name
    
    def table(self, table_name: str):
        """Create table query builder in specified schema"""
        full_table_name = f"{self.schema_name}.{table_name}"
        return PostgreSQLTable(self.pool, full_table_name)

    # Supabase compatibility helper
    def from_(self, table_name: str):
        """Alias for table() to mirror Supabase API"""
        return self.table(table_name)

class PostgreSQLTable:
    """PostgreSQL table query builder providing database operation interfaces"""
    
    def __init__(self, pool: asyncpg.Pool, table_name: str):
        self.pool = pool
        self.table_name = table_name
        self._select_fields = "*"
        self._where_conditions = []
        self._order_by = []
        self._limit_value = None
        self._offset_value = None
        self._count_flag = False
        self._params = []
        self._single_result = False
        self._maybe_single = False
    
    def select(self, fields: str = "*", count: str = None):
        """Select specific fields"""
        self._select_fields = fields
        if count == "exact":
            self._count_flag = True
        return self
    
    def eq(self, column: str, value: Any):
        """Add equality condition"""
        self._where_conditions.append(f"{column} = ${len(self._params) + 1}")
        self._params.append(value)
        return self
    
    def neq(self, column: str, value: Any):
        """Add not equal condition (supports .neq() method)"""
        if value is None:
            self._where_conditions.append(f"{column} IS NOT NULL")
        else:
            self._where_conditions.append(f"{column} != ${len(self._params) + 1}")
            self._params.append(value)
        return self
    
    def lt(self, column: str, value: Any):
        """Add less than condition"""
        self._where_conditions.append(f"{column} < ${len(self._params) + 1}")
        self._params.append(value)
        return self
    
    def gt(self, column: str, value: Any):
        """Add greater than condition"""
        self._where_conditions.append(f"{column} > ${len(self._params) + 1}")
        self._params.append(value)
        return self
    
    def gte(self, column: str, value: Any):
        """Add greater than or equal condition"""
        self._where_conditions.append(f"{column} >= ${len(self._params) + 1}")
        self._params.append(value)
        return self
    
    def lte(self, column: str, value: Any):
        """Add less than or equal condition"""
        self._where_conditions.append(f"{column} <= ${len(self._params) + 1}")
        self._params.append(value)
        return self
    
    def like(self, column: str, pattern: str):
        """Add LIKE condition"""
        self._where_conditions.append(f"{column} LIKE ${len(self._params) + 1}")
        self._params.append(pattern)
        return self
    
    def ilike(self, column: str, pattern: str):
        """Add case-insensitive LIKE condition"""
        self._where_conditions.append(f"{column} ILIKE ${len(self._params) + 1}")
        self._params.append(pattern)
        return self
    
    def contains(self, column: str, value: Any):
        """Add contains condition (for array or JSON fields)"""
        if isinstance(value, (list, dict)):
            # For array or JSONB fields, use @> operator
            self._where_conditions.append(f"{column} @> ${len(self._params) + 1}::jsonb")
            self._params.append(json.dumps(value))
        else:
            # For text search, use LIKE
            self._where_conditions.append(f"{column} LIKE ${len(self._params) + 1}")
            self._params.append(f"%{value}%")
        return self
    
    def in_(self, column: str, values: List[Any]):
        """Add IN condition"""
        if not values:
            # If list is empty, add a condition that is always false
            self._where_conditions.append("1 = 0")
            return self
        
        placeholders = []
        for value in values:
            self._params.append(value)
            placeholders.append(f"${len(self._params)}")
        
        self._where_conditions.append(f"{column} IN ({', '.join(placeholders)})")
        return self
    
    def is_(self, column: str, value: Any):
        """Add IS condition (for NULL checks)"""
        if value is None:
            self._where_conditions.append(f"{column} IS NULL")
        else:
            self._where_conditions.append(f"{column} IS ${len(self._params) + 1}")
            self._params.append(value)
        return self
    
    @property
    def not_(self):
        """Return NOT query builder"""
        return PostgreSQLNotBuilder(self)
    
    def filter(self, field_expression: str, operator: str, value: Any):
        """Add filter condition (supports Supabase filter syntax)"""
        if operator == 'eq':
            return self.eq(field_expression, value)
        elif operator == 'neq':
            return self.neq(field_expression, value)
        elif operator == 'lt':
            return self.lt(field_expression, value)
        elif operator == 'gt':
            return self.gt(field_expression, value)
        # For complex JSON field queries like 'sandbox->>id'
        elif '->>' in field_expression:
            self._where_conditions.append(f"{field_expression} = ${len(self._params) + 1}")
            self._params.append(value)
        else:
            logger.warning(f"Unsupported filter operator: {operator}")
        return self
    
    def or_(self, condition: str):
        """Add OR condition (simplified implementation)"""
        # Handle basic ilike search
        if "ilike" in condition:
            # Parse conditions like "name.ilike.%search%,description.ilike.%search%"
            parts = condition.split(",")
            or_conditions = []
            for part in parts:
                if ".ilike." in part:
                    field, _, pattern = part.split(".", 2)
                    or_conditions.append(f"{field} ILIKE ${len(self._params) + 1}")
                    self._params.append(pattern)
            
            if or_conditions:
                self._where_conditions.append(f"({' OR '.join(or_conditions)})")
        return self
    
    def order(self, column: str, desc: bool = False):
        """Add ORDER BY clause"""
        direction = "DESC" if desc else "ASC"
        self._order_by.append(f"{column} {direction}")
        return self
    
    def range(self, start: int, end: int):
        """Add pagination (LIMIT and OFFSET)"""
        self._limit_value = end - start + 1
        self._offset_value = start
        return self
    
    def limit(self, count: int):
        """Add LIMIT clause"""
        self._limit_value = count
        return self
    
    def single(self):
        """Mark query should return single result"""
        self._single_result = True
        self._limit_value = 1
        return self
    
    def maybe_single(self):
        """Mark query may return single result or null"""
        self._maybe_single = True
        self._limit_value = 1
        return self
    
    async def execute(self):
        """Execute query"""
        # Build SELECT query
        query_parts = [f"SELECT {self._select_fields}"]
        
        # If count is needed, build count query
        count_query = None
        if self._count_flag:
            count_query = f"SELECT COUNT(*) FROM {self.table_name}"
            if self._where_conditions:
                count_query += f" WHERE {' AND '.join(self._where_conditions)}"
        
        query_parts.append(f"FROM {self.table_name}")
        
        # Add WHERE clause
        if self._where_conditions:
            query_parts.append(f"WHERE {' AND '.join(self._where_conditions)}")
        
        # Add ORDER BY
        if self._order_by:
            query_parts.append(f"ORDER BY {', '.join(self._order_by)}")
        
        # Add LIMIT and OFFSET
        if self._limit_value:
            query_parts.append(f"LIMIT {self._limit_value}")
        if self._offset_value:
            query_parts.append(f"OFFSET {self._offset_value}")
        
        query = " ".join(query_parts)
        
        try:
            async with self.pool.acquire() as conn:
                # Execute main query
                rows = await conn.fetch(query, *self._params)
                data = [dict(row) for row in rows]
                
                # If count is needed, execute count query
                count = None
                if self._count_flag:
                    count_result = await conn.fetchval(count_query, *self._params)
                    count = int(count_result) if count_result else 0
                
                # Handle single and maybe_single cases
                if self._single_result:
                    if not data:
                        raise ValueError("Query returned no results")
                    return QueryResult(data[0], count)
                elif self._maybe_single:
                    if not data:
                        return QueryResult(None, count)
                    return QueryResult(data[0], count)
                
                # Return Supabase-style result
                return QueryResult(data, count)
                
        except Exception as e:
            logger.error(f"Query execution failed: {e}")
            raise RuntimeError(f"Database query failed: {str(e)}")
    
    async def insert(self, data: Union[Dict[str, Any], List[Dict[str, Any]]]):
        """Insert data into table"""
        try:
            # Handle single record and multiple records
            if isinstance(data, dict):
                data = [data]
            
            if not data:
                return QueryResult([])
            
            # Get all field names
            columns = list(data[0].keys())
            
            # Build insert query
            values_placeholders = []
            all_values = []
            
            for i, record in enumerate(data):
                record_placeholders = []
                for j, column in enumerate(columns):
                    placeholder_index = i * len(columns) + j + 1
                    record_placeholders.append(f"${placeholder_index}")
                    all_values.append(record[column])
                values_placeholders.append(f"({', '.join(record_placeholders)})")
            
            query = f"""
            INSERT INTO {self.table_name} ({', '.join(columns)})
            VALUES {', '.join(values_placeholders)}
            RETURNING *
            """
            
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, *all_values)
                result_data = [dict(row) for row in rows]
                return QueryResult(result_data)
                
        except Exception as e:
            logger.error(f"Insert operation failed: {e}")
            raise RuntimeError(f"Database insert failed: {str(e)}")
    
    async def update(self, data: Dict[str, Any]):
        """Update data in table"""
        try:
            # Build SET clause
            set_clauses = []
            values = []
            for key, value in data.items():
                set_clauses.append(f"{key} = ${len(values) + len(self._params) + 1}")
                values.append(value)
            
            query_parts = [f"UPDATE {self.table_name}"]
            query_parts.append(f"SET {', '.join(set_clauses)}")
            
            if self._where_conditions:
                query_parts.append(f"WHERE {' AND '.join(self._where_conditions)}")
            
            query_parts.append("RETURNING *")
            query = " ".join(query_parts)
            
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, *(self._params + values))
                result_data = [dict(row) for row in rows]
                
                # Handle single result case
                if self._single_result or self._maybe_single:
                    if not result_data and self._single_result:
                        raise ValueError("Update operation affected no records")
                    return QueryResult(result_data[0] if result_data else None)
                
                return QueryResult(result_data)
                
        except Exception as e:
            logger.error(f"Update operation failed: {e}")
            raise RuntimeError(f"Database update failed: {str(e)}")
    
    async def delete(self):
        """Delete data from table"""
        try:
            query_parts = [f"DELETE FROM {self.table_name}"]
            
            if self._where_conditions:
                query_parts.append(f"WHERE {' AND '.join(self._where_conditions)}")
            
            query_parts.append("RETURNING *")
            query = " ".join(query_parts)
            
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, *self._params)
                result_data = [dict(row) for row in rows]
                return QueryResult(result_data)
                
        except Exception as e:
            logger.error(f"Delete operation failed: {e}")
            raise RuntimeError(f"Database delete failed: {str(e)}")

class QueryResult:
    """Query result wrapper matching Supabase interface"""
    
    def __init__(self, data: Union[List[Dict[str, Any]], Dict[str, Any], None], count: Optional[int] = None):
        self.data = data
        self.count = count
