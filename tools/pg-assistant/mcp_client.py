"""MCP (Model Context Protocol) client for PostgreSQL server communication."""

import logging
import time
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_MCP_URL = "http://localhost:3000"
DEFAULT_TIMEOUT = 30


class MCPClient:
    """Client for interacting with the MCP PostgreSQL server."""

    def __init__(
        self,
        base_url: str = DEFAULT_MCP_URL,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def execute_query(self, sql: str) -> dict[str, Any]:
        """Execute a SQL query via the MCP server.

        Args:
            sql: The SQL query string to execute.

        Returns:
            A dict with keys 'columns' and 'rows' on success,
            or 'error' on failure.

        Raises:
            ConnectionError: If the MCP server is unreachable.
            RuntimeError: If the MCP server returns an error response.
        """
        payload = {
            "method": "query",
            "params": {"sql": sql},
        }

        logger.debug("Sending query to MCP server: %s", sql[:200])
        start = time.monotonic()

        try:
            response = requests.post(
                self.base_url,
                json=payload,
                timeout=self.timeout,
            )
        except requests.ConnectionError as exc:
            logger.error("Cannot reach MCP server at %s", self.base_url)
            raise ConnectionError(
                f"Cannot connect to MCP server at {self.base_url}. "
                "Ensure the MCP PostgreSQL server is running."
            ) from exc
        except requests.Timeout as exc:
            logger.error("MCP request timed out after %ds", self.timeout)
            raise RuntimeError(
                f"MCP server request timed out after {self.timeout}s."
            ) from exc

        elapsed = time.monotonic() - start
        logger.debug("MCP server responded in %.2fs", elapsed)

        if response.status_code != 200:
            error_detail = response.text[:500]
            logger.error("MCP server error %d: %s", response.status_code, error_detail)
            raise RuntimeError(
                f"MCP server returned status {response.status_code}: {error_detail}"
            )

        data: dict[str, Any] = response.json()

        if "error" in data:
            error_msg = data["error"]
            logger.warning("MCP query error: %s", error_msg)
            return {"error": str(error_msg)}

        return {
            "columns": data.get("columns", []),
            "rows": data.get("rows", []),
            "row_count": data.get("rowCount", len(data.get("rows", []))),
            "elapsed_ms": round(elapsed * 1000, 2),
        }

    def get_schema(self, schema_name: str = "public") -> Optional[dict[str, Any]]:
        """Retrieve database schema metadata via the MCP server.

        Args:
            schema_name: The PostgreSQL schema to inspect.

        Returns:
            Schema metadata dict, or None on failure.
        """
        sql = f"""
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
            WHERE t.table_schema = '{schema_name}'
                AND t.table_type = 'BASE TABLE'
            ORDER BY t.table_name, c.ordinal_position;
        """
        try:
            result = self.execute_query(sql)
            if "error" in result:
                logger.warning("Failed to fetch schema: %s", result["error"])
                return None
            return self._parse_schema(result)
        except (ConnectionError, RuntimeError) as exc:
            logger.warning("Failed to fetch schema: %s", exc)
            return None

    def health_check(self) -> bool:
        """Check whether the MCP server is reachable.

        Returns:
            True if the server responds, False otherwise.
        """
        try:
            response = requests.get(self.base_url, timeout=5)
            return response.status_code < 500
        except (requests.ConnectionError, requests.Timeout):
            return False

    @staticmethod
    def _parse_schema(result: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
        """Parse raw schema query results into a structured dict.

        Args:
            result: The raw query result from execute_query.

        Returns:
            A dict mapping table names to lists of column info dicts.
        """
        schema: dict[str, list[dict[str, str]]] = {}
        columns = result.get("columns", [])
        rows = result.get("rows", [])

        for row in rows:
            if isinstance(row, dict):
                table = row.get("table_name", "")
                col_info = {
                    "column_name": row.get("column_name", ""),
                    "data_type": row.get("data_type", ""),
                    "is_nullable": row.get("is_nullable", ""),
                    "column_default": row.get("column_default", ""),
                }
            elif isinstance(row, (list, tuple)) and len(columns) >= 5:
                table = str(row[0])
                col_info = {
                    "column_name": str(row[1]),
                    "data_type": str(row[2]),
                    "is_nullable": str(row[3]),
                    "column_default": str(row[4]) if row[4] else "",
                }
            else:
                continue

            if table not in schema:
                schema[table] = []
            schema[table].append(col_info)

        return schema
