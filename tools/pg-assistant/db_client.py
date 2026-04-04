"""Database client supporting PostgreSQL and Oracle connections."""

import abc
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)

DB_TYPE_POSTGRESQL = "postgresql"
DB_TYPE_ORACLE = "oracle"
SUPPORTED_DB_TYPES = (DB_TYPE_POSTGRESQL, DB_TYPE_ORACLE)

# Conditional imports -- only the driver for the chosen DB type is required.
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None  # type: ignore[assignment]

try:
    import oracledb
except ImportError:
    oracledb = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------
class BaseDBClient(abc.ABC):
    """Common interface for all database clients."""

    @abc.abstractmethod
    def connect(self) -> None: ...

    @abc.abstractmethod
    def disconnect(self) -> None: ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abc.abstractmethod
    def db_type(self) -> str: ...

    @abc.abstractmethod
    def execute_query(self, sql: str) -> dict[str, Any]:
        """Execute a SELECT query and return columns/rows/row_count/elapsed_ms."""

    @abc.abstractmethod
    def execute_statement(self, sql: str) -> dict[str, Any]:
        """Execute DDL/DML (no result set). Returns success/error/elapsed_ms."""

    @abc.abstractmethod
    def get_schema(
        self, schema_name: str = ""
    ) -> Optional[dict[str, list[dict[str, str]]]]: ...

    @abc.abstractmethod
    def get_connection_info(self) -> str: ...


# ---------------------------------------------------------------------------
# PostgreSQL
# ---------------------------------------------------------------------------
class PostgreSQLClient(BaseDBClient):
    """Client for PostgreSQL via psycopg2."""

    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        user: str,
        password: str,
        sslmode: str = "prefer",
    ) -> None:
        if psycopg2 is None:
            raise ImportError(
                "psycopg2 is required for PostgreSQL connections. "
                "Install it with: pip install psycopg2-binary"
            )
        self.conn_params: dict[str, Any] = {
            "host": host,
            "port": port,
            "dbname": database,
            "user": user,
            "password": password,
            "sslmode": sslmode,
        }
        self._conn: Any = None

    @property
    def db_type(self) -> str:
        return DB_TYPE_POSTGRESQL

    def connect(self) -> None:
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
            raise ConnectionError(f"Cannot connect to PostgreSQL: {exc}") from exc

    def disconnect(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
            logger.info("Disconnected from PostgreSQL")

    @property
    def is_connected(self) -> bool:
        if self._conn is None or self._conn.closed:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False

    def execute_query(self, sql: str) -> dict[str, Any]:
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
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("Query execution failed: %s", exc)
            return {"error": str(exc).strip(), "elapsed_ms": round(elapsed * 1000, 2)}

    def execute_statement(self, sql: str) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Not connected to PostgreSQL. Please connect first.")
        start = time.monotonic()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql)
            elapsed = time.monotonic() - start
            return {"success": True, "elapsed_ms": round(elapsed * 1000, 2)}
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("Statement execution failed: %s", exc)
            return {
                "success": False,
                "error": str(exc).strip(),
                "elapsed_ms": round(elapsed * 1000, 2),
            }

    def get_schema(
        self, schema_name: str = "public"
    ) -> Optional[dict[str, list[dict[str, str]]]]:
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
        except Exception as exc:
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
            schema.setdefault(table, []).append(col_info)
        return schema

    def get_connection_info(self) -> str:
        p = self.conn_params
        return f"{p['user']}@{p['host']}:{p['port']}/{p['dbname']}"


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------
class OracleClient(BaseDBClient):
    """Client for Oracle via python-oracledb (thin mode, no Oracle Client needed)."""

    def __init__(
        self,
        host: str,
        port: int,
        service_name: str,
        user: str,
        password: str,
    ) -> None:
        if oracledb is None:
            raise ImportError(
                "oracledb is required for Oracle connections. "
                "Install it with: pip install oracledb"
            )
        self._host = host
        self._port = port
        self._service_name = service_name
        self._user = user
        self._password = password
        self._dsn = f"{host}:{port}/{service_name}"
        self._conn: Any = None

    @property
    def db_type(self) -> str:
        return DB_TYPE_ORACLE

    def connect(self) -> None:
        try:
            self._conn = oracledb.connect(
                user=self._user, password=self._password, dsn=self._dsn
            )
            logger.info("Connected to Oracle at %s", self._dsn)
        except oracledb.Error as exc:
            raise ConnectionError(f"Cannot connect to Oracle: {exc}") from exc

    def disconnect(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
                logger.info("Disconnected from Oracle")
            except Exception:
                pass
            self._conn = None

    @property
    def is_connected(self) -> bool:
        if self._conn is None:
            return False
        try:
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1 FROM DUAL")
            return True
        except Exception:
            return False

    def execute_query(self, sql: str) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Not connected to Oracle. Please connect first.")
        start = time.monotonic()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql)
                if cur.description:
                    columns = [desc[0] for desc in cur.description]
                    raw_rows = cur.fetchall()
                    elapsed = time.monotonic() - start
                    rows = [dict(zip(columns, r)) for r in raw_rows]
                    return {
                        "columns": columns,
                        "rows": rows,
                        "row_count": len(rows),
                        "elapsed_ms": round(elapsed * 1000, 2),
                    }
                elapsed = time.monotonic() - start
                return {
                    "columns": [],
                    "rows": [],
                    "row_count": 0,
                    "elapsed_ms": round(elapsed * 1000, 2),
                }
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("Query execution failed: %s", exc)
            return {"error": str(exc).strip(), "elapsed_ms": round(elapsed * 1000, 2)}

    def execute_statement(self, sql: str) -> dict[str, Any]:
        if not self.is_connected:
            raise ConnectionError("Not connected to Oracle. Please connect first.")
        start = time.monotonic()
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql)
            self._conn.commit()
            elapsed = time.monotonic() - start
            return {"success": True, "elapsed_ms": round(elapsed * 1000, 2)}
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("Statement execution failed: %s", exc)
            return {
                "success": False,
                "error": str(exc).strip(),
                "elapsed_ms": round(elapsed * 1000, 2),
            }

    def get_schema(
        self, schema_name: str = ""
    ) -> Optional[dict[str, list[dict[str, str]]]]:
        if not schema_name:
            schema_name = self._user.upper()
        sql = """
            SELECT table_name, column_name, data_type, nullable, data_default
            FROM all_tab_columns
            WHERE owner = :owner
            ORDER BY table_name, column_id
        """
        if not self.is_connected:
            return None
        try:
            with self._conn.cursor() as cur:
                cur.execute(sql, {"owner": schema_name})
                raw_rows = cur.fetchall()
        except Exception as exc:
            logger.warning("Failed to fetch Oracle schema: %s", exc)
            return None

        schema: dict[str, list[dict[str, str]]] = {}
        for row in raw_rows:
            table = row[0]
            col_info = {
                "column_name": row[1],
                "data_type": row[2],
                "is_nullable": "YES" if row[3] == "Y" else "NO",
                "column_default": str(row[4]) if row[4] else "",
            }
            schema.setdefault(table, []).append(col_info)
        return schema

    def get_connection_info(self) -> str:
        return f"{self._user}@{self._dsn}"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------
def create_db_client(db_type: str, **kwargs: Any) -> BaseDBClient:
    """Create a database client for the given type.

    Args:
        db_type: One of 'postgresql' or 'oracle'.
        **kwargs: Connection parameters forwarded to the client constructor.
    """
    if db_type == DB_TYPE_POSTGRESQL:
        return PostgreSQLClient(
            host=kwargs["host"],
            port=kwargs["port"],
            database=kwargs["database"],
            user=kwargs["user"],
            password=kwargs["password"],
            sslmode=kwargs.get("sslmode", "prefer"),
        )
    if db_type == DB_TYPE_ORACLE:
        return OracleClient(
            host=kwargs["host"],
            port=kwargs["port"],
            service_name=kwargs["service_name"],
            user=kwargs["user"],
            password=kwargs["password"],
        )
    raise ValueError(
        f"Unsupported database type: {db_type!r}. Supported: {SUPPORTED_DB_TYPES}"
    )
