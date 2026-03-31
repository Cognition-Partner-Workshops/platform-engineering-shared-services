"""Oracle Database connection manager using python-oracledb (thin mode)."""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

import oracledb

from oracle_db_agent.config import AgentConfig

logger = logging.getLogger("oracle_db_agent.connection")


class OracleConnectionManager:
    """Manages Oracle DB connections with pooling and retry logic."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._pool: oracledb.ConnectionPool | None = None

    def initialize_pool(self) -> None:
        """Create the connection pool."""
        db = self._config.database
        logger.info("Initializing connection pool: %s@%s", db.username, db.dsn)
        self._pool = oracledb.create_pool(
            user=db.username,
            password=db.password,
            dsn=db.dsn,
            min=db.pool_min,
            max=db.pool_max,
            increment=db.pool_increment,
        )
        logger.info("Connection pool initialized (min=%d, max=%d)", db.pool_min, db.pool_max)

    def close_pool(self) -> None:
        """Close the connection pool."""
        if self._pool is not None:
            self._pool.close(force=True)
            self._pool = None
            logger.info("Connection pool closed.")

    @contextmanager
    def get_connection(self) -> Generator[oracledb.Connection, None, None]:
        """Get a connection from the pool as a context manager."""
        if self._pool is None:
            self.initialize_pool()
        assert self._pool is not None
        conn = self._pool.acquire()
        try:
            yield conn
        finally:
            self._pool.release(conn)

    def execute_query(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[tuple[Any, ...]]:
        """Execute a SELECT query and return all rows."""
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or {})
            rows: list[tuple[Any, ...]] = cur.fetchall()
            return rows

    def execute_query_with_columns(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> tuple[list[str], list[tuple[Any, ...]]]:
        """Execute a SELECT query and return column names and rows."""
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or {})
            columns = [desc[0] for desc in cur.description] if cur.description else []
            rows: list[tuple[Any, ...]] = cur.fetchall()
            return columns, rows

    def execute_dml(self, sql: str, params: dict[str, Any] | None = None) -> int:
        """Execute a DML statement (INSERT/UPDATE/DELETE) and return rows affected."""
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(sql, params or {})
            conn.commit()
            return cur.rowcount

    def execute_ddl(self, sql: str) -> None:
        """Execute a DDL statement (ALTER, CREATE, DROP, etc.)."""
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(sql)
            logger.info("DDL executed: %s", sql[:200])

    def execute_plsql(self, plsql: str, params: dict[str, Any] | None = None) -> None:
        """Execute a PL/SQL block."""
        with self.get_connection() as conn, conn.cursor() as cur:
            cur.execute(plsql, params or {})
            conn.commit()
            logger.info("PL/SQL executed: %s", plsql[:200])

    def test_connection(self) -> bool:
        """Test database connectivity."""
        try:
            rows = self.execute_query("SELECT 1 FROM DUAL")
            return len(rows) > 0 and rows[0][0] == 1
        except Exception as exc:
            logger.error("Connection test failed: %s", exc)
            return False
