"""
Thread Manager Adapter for AgentScope Integration

This module provides a lightweight adapter that mimics the interface
of ADKThreadManager, specifically the `db` property that tool classes need.
"""

from services.postgresql import DBConnection


class ThreadManagerAdapter:
    """
    Lightweight adapter that provides the `db` interface needed by tool classes.
    
    This adapter allows existing tool classes (SandboxCodeTool, etc.) to work
    with AgentScope without modification. Tool classes access the database
    through `thread_manager.db.client`, and this adapter provides that interface.
    """
    
    def __init__(self, db_client=None):
        """
        Initialize the adapter.
        
        Args:
            db_client: Optional pre-initialized database client.
                      If not provided, a new DBConnection will be created.
        """
        self._db_client = db_client
        self._db = None
    
    @property
    def db(self):
        """
        Get the database connection object.
        
        Returns a DBConnection instance that provides the `client` property.
        """
        if self._db is None:
            self._db = _DBWrapper(self._db_client)
        return self._db


class _DBWrapper:
    """
    Wrapper that provides the `client` property interface.
    
    This mimics the DBConnection interface where `client` is an async property.
    """
    
    def __init__(self, db_client=None):
        self._db_client = db_client
        self._db_connection = None
    
    @property
    async def client(self):
        """
        Get the database client.
        
        If a pre-initialized client was provided, return it.
        Otherwise, create a new DBConnection and return its client.
        """
        if self._db_client is not None:
            return self._db_client
        
        if self._db_connection is None:
            self._db_connection = DBConnection()
        
        return await self._db_connection.client
