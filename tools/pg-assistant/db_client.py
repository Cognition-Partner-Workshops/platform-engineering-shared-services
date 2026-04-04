"""Direct PostgreSQL database client using psycopg2."""

import logging
import time
from typing import Any, Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


class DBClient:
    """Client for direct PostgreSQL database connections."""

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        sslmode: str = "prefer",
    ) -> None:
        self.conn_params = {
            "host": host,
            "port": port,
            "dbname": database,
            "user": user,
            "password": password,
            "sslmode": sslmode,
        }
        self._conn: Optional[psycopg2.extensions.connection] = None

    def connect(self) -> None:
        """Establish a connection to PostgreSQL.

        Raises:
            ConnectionError: If the database is unreachable.
        """
        try:
            self._conn = psycopg2.connect(**self.conn_params)
            self._conn.autocommit = True
            logger.info(
                "Connected to PostgreSQL at %s:%s/%s",
                self.conn_params["host"],
                self.conn_params["port"],
                self.conn_params["dbname"],
            )
        except psycopg2.OperationalError as exc:
            logger.error("Failed to connect to PostgreSQL: %s", exc)
            raise ConnectionError(f"Cannot connect to PostgreSQL: {exc}") from exc

    def disconnect(self) -> None:
        """Close the database connection."""
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("Disconnected from PostgreSQL")

    @property
    def is_connected(self) -> bool:
        """Check whether the connection is active."""
        if self._conn is None or self._conn.closed:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except psycopg2.Error:
            return False

    def execute_query(self, sql: str) -> dict[str, Any]:
        """Execute a SQL query and return results.

        Args:
            sql: The SQL query string to execute.

        Returns:
            A dict with 'columns', 'rows', 'row_count', and 'elapsed_ms'.

        Raises:
            ConnectionError: If not connected to the database.
            RuntimeError: If the query fails.
        """
        if not self.is_connected:
            raise ConnectionError("Not connected to PostgreSQL. Please connect first.")

        start = time.monotonic()
        try:
            with self._conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql)
                columns = (
                    [desc[0] for desc in cur.description] if cur.description else []
                )
                rows = cur.fetchall() if cur.description else []
                elapsed = time.monotonic() - start
                return {
                    "columns": columns,
                    "rows": [dict(row) for row in rows],
                    "row_count": len(rows),
                    "elapsed_ms": round(elapsed * 1000, 2),
                }
        except psycopg2.Error as exc:
            elapsed = time.monotonic() - start
            logger.error("Query execution failed: %s", exc)
            return {
                "error": str(exc).strip(),
                "elapsed_ms": round(elapsed * 1000, 2),
            }

    def get_schema(
        self, schema_name: str = "public"
    ) -> Optional[dict[str, list[dict[str, str]]]]:
        """Retrieve database schema metadata.

        Args:
            schema_name: The PostgreSQL schema to inspect.

        Returns:
            Schema metadata dict mapping table names to column info lists,
            or None on failure.
        """
        sql = """
            SELECT
                t.table_name,
                c.column_name,
                c.data_type,
                c.is_nullable,
                c.column_default
            FROM information_schema.tables t
            JOIN information_schema.columns c
                ON t.table_name = c.table_name
                AND t.table_schema = c.table_schema
            WHERE t.table_schema = %s
                AND t.table_type = 'BASE TABLE'
            ORDER BY t.table_name, c.ordinal_position;
        """
        if not self.is_connected:
            return None

        try:
            with self._conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            ) as cur:
                cur.execute(sql, (schema_name,))
                rows = cur.fetchall()
        except psycopg2.Error as exc:
            logger.warning("Failed to fetch schema: %s", exc)
            return None

        schema: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            table = row["table_name"]
            col_info = {
                "column_name": row["column_name"],
                "data_type": row["data_type"],
                "is_nullable": row["is_nullable"],
                "column_default": row["column_default"] or "",
            }
            if table not in schema:
                schema[table] = []
            schema[table].append(col_info)

        return schema

    def get_connection_info(self) -> str:
        """Return a display-friendly connection string (password masked)."""
        return (
            f"{self.conn_params['user']}@"
            f"{self.conn_params['host']}:{self.conn_params['port']}/"
            f"{self.conn_params['dbname']}"
        )
