"""Tablespace monitoring with auto-extend support for Oracle and PostgreSQL."""

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from db_client import BaseDBClient, DB_TYPE_ORACLE

logger = logging.getLogger(__name__)

DEFAULT_THRESHOLD_PCT = 85
DEFAULT_MAX_FILE_SIZE_GB = 20
DEFAULT_INTERVAL_SEC = 3600  # 1 hour


# ---------------------------------------------------------------------------
# Oracle tablespace queries
# ---------------------------------------------------------------------------
_ORACLE_TABLESPACE_USAGE_SQL = """
    SELECT
        df.tablespace_name,
        COUNT(df.file_id) AS file_count,
        ROUND(SUM(df.bytes) / 1048576, 2) AS total_size_mb,
        ROUND(NVL(SUM(fs.free_bytes), 0) / 1048576, 2) AS free_mb,
        ROUND((SUM(df.bytes) - NVL(SUM(fs.free_bytes), 0)) / 1048576, 2) AS used_mb,
        ROUND(
            (SUM(df.bytes) - NVL(SUM(fs.free_bytes), 0)) / SUM(df.bytes) * 100, 2
        ) AS used_pct
    FROM dba_data_files df
    LEFT JOIN (
        SELECT file_id, SUM(bytes) AS free_bytes
        FROM dba_free_space
        GROUP BY file_id
    ) fs ON df.file_id = fs.file_id
    GROUP BY df.tablespace_name
    ORDER BY used_pct DESC
"""

_ORACLE_DATAFILES_SQL = """
    SELECT
        file_id,
        file_name,
        tablespace_name,
        ROUND(bytes / 1048576, 2) AS size_mb,
        ROUND(maxbytes / 1048576, 2) AS max_size_mb,
        autoextensible
    FROM dba_data_files
    WHERE tablespace_name = :ts_name
    ORDER BY file_id
"""

# ---------------------------------------------------------------------------
# PostgreSQL storage queries
# ---------------------------------------------------------------------------
_PG_DATABASE_SIZE_SQL = """
    SELECT
        datname AS database_name,
        pg_database_size(datname) / 1048576 AS size_mb
    FROM pg_database
    WHERE datname NOT IN ('template0', 'template1')
    ORDER BY size_mb DESC
"""

_PG_TABLE_SIZE_SQL = """
    SELECT
        schemaname,
        tablename,
        pg_total_relation_size(quote_ident(schemaname) || '.' || quote_ident(tablename)) / 1048576 AS total_size_mb,
        pg_relation_size(quote_ident(schemaname) || '.' || quote_ident(tablename)) / 1048576 AS table_size_mb
    FROM pg_tables
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY total_size_mb DESC
    LIMIT 50
"""

_PG_TABLESPACE_SQL = """
    SELECT
        spcname AS tablespace_name,
        pg_tablespace_location(oid) AS location,
        pg_tablespace_size(oid) / 1048576 AS size_mb
    FROM pg_tablespace
    ORDER BY size_mb DESC
"""


# ---------------------------------------------------------------------------
# Monitor event dataclass-like dict builder
# ---------------------------------------------------------------------------
def _event(
    status: str,
    tablespace_data: list[dict[str, Any]],
    actions: list[dict[str, Any]],
    error: str = "",
) -> dict[str, Any]:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "tablespace_data": tablespace_data,
        "actions": actions,
        "error": error,
    }


# ---------------------------------------------------------------------------
# Core monitor logic
# ---------------------------------------------------------------------------
class TablespaceMonitor:
    """Monitors tablespace usage and auto-extends datafiles when needed."""

    def __init__(
        self,
        db_client: BaseDBClient,
        threshold_pct: float = DEFAULT_THRESHOLD_PCT,
        max_file_size_gb: float = DEFAULT_MAX_FILE_SIZE_GB,
        interval_sec: int = DEFAULT_INTERVAL_SEC,
    ) -> None:
        self.db_client = db_client
        self.threshold_pct = threshold_pct
        self.max_file_size_gb = max_file_size_gb
        self.max_file_size_mb = max_file_size_gb * 1024
        self.interval_sec = interval_sec
        self.events: list[dict[str, Any]] = []
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.running = False

    # -- public API ----------------------------------------------------------

    def start(self) -> None:
        """Start periodic monitoring in a background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.running = True
        logger.info(
            "Tablespace monitor started (interval=%ds, threshold=%s%%)",
            self.interval_sec,
            self.threshold_pct,
        )

    def stop(self) -> None:
        """Stop the background monitoring thread."""
        self._stop.set()
        self.running = False
        logger.info("Tablespace monitor stopped")

    def run_check(self) -> dict[str, Any]:
        """Run a single monitoring check and return the event dict."""
        try:
            if self.db_client.db_type == DB_TYPE_ORACLE:
                return self._check_oracle()
            return self._check_postgresql()
        except Exception as exc:
            event = _event("error", [], [], error=str(exc))
            self.events.append(event)
            return event

    # -- background loop -----------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_check()
            except Exception as exc:
                logger.error("Monitor check failed: %s", exc)
            self._stop.wait(self.interval_sec)
        self.running = False

    # -- Oracle checks -------------------------------------------------------

    def _check_oracle(self) -> dict[str, Any]:
        result = self.db_client.execute_query(_ORACLE_TABLESPACE_USAGE_SQL)
        if "error" in result:
            event = _event("error", [], [], error=result["error"])
            self.events.append(event)
            return event

        ts_data = result.get("rows", [])
        actions: list[dict[str, Any]] = []

        for ts in ts_data:
            ts_name = ts.get("TABLESPACE_NAME") or ts.get("tablespace_name", "")
            used_pct = float(ts.get("USED_PCT") or ts.get("used_pct", 0))

            if used_pct >= self.threshold_pct:
                ts_actions = self._auto_extend_oracle(ts_name)
                actions.extend(ts_actions)

        status = "warning" if actions else "ok"
        event = _event(status, ts_data, actions)
        self.events.append(event)
        return event

    def _auto_extend_oracle(self, tablespace_name: str) -> list[dict[str, Any]]:
        """Attempt to auto-extend or add datafiles for an Oracle tablespace."""
        actions: list[dict[str, Any]] = []

        df_result = self.db_client.execute_query(
            _ORACLE_DATAFILES_SQL.replace(":ts_name", f"'{tablespace_name}'")
        )
        if "error" in df_result:
            actions.append(
                {
                    "tablespace": tablespace_name,
                    "action": "error",
                    "detail": f"Failed to query datafiles: {df_result['error']}",
                }
            )
            return actions

        datafiles = df_result.get("rows", [])
        extended_any = False

        for df in datafiles:
            file_name = df.get("FILE_NAME") or df.get("file_name", "")
            max_size_mb = float(df.get("MAX_SIZE_MB") or df.get("max_size_mb", 0))
            autoext = df.get("AUTOEXTENSIBLE") or df.get("autoextensible", "NO")

            if autoext == "YES" and max_size_mb >= self.max_file_size_mb:
                continue

            if autoext != "YES" or max_size_mb < self.max_file_size_mb:
                max_mb = int(self.max_file_size_mb)
                sql = (
                    f"ALTER DATABASE DATAFILE '{file_name}' "
                    f"AUTOEXTEND ON MAXSIZE {max_mb}M"
                )
                stmt_result = self.db_client.execute_statement(sql)
                if stmt_result.get("success"):
                    actions.append(
                        {
                            "tablespace": tablespace_name,
                            "action": "autoextend_enabled",
                            "file": file_name,
                            "max_size_mb": max_mb,
                            "sql": sql,
                        }
                    )
                    extended_any = True
                else:
                    actions.append(
                        {
                            "tablespace": tablespace_name,
                            "action": "autoextend_failed",
                            "file": file_name,
                            "error": stmt_result.get("error", "unknown"),
                            "sql": sql,
                        }
                    )

        if not extended_any:
            add_sql = (
                f"ALTER TABLESPACE {tablespace_name} ADD DATAFILE "
                f"SIZE 1024M AUTOEXTEND ON MAXSIZE {int(self.max_file_size_mb)}M"
            )
            stmt_result = self.db_client.execute_statement(add_sql)
            if stmt_result.get("success"):
                actions.append(
                    {
                        "tablespace": tablespace_name,
                        "action": "datafile_added",
                        "sql": add_sql,
                    }
                )
            else:
                dir_path = self._derive_datafile_dir(datafiles)
                if dir_path:
                    new_name = os.path.join(
                        dir_path,
                        f"{tablespace_name.lower()}_auto_{len(datafiles) + 1:02d}.dbf",
                    )
                    add_sql2 = (
                        f"ALTER TABLESPACE {tablespace_name} ADD DATAFILE "
                        f"'{new_name}' SIZE 1024M AUTOEXTEND ON "
                        f"MAXSIZE {int(self.max_file_size_mb)}M"
                    )
                    stmt_result2 = self.db_client.execute_statement(add_sql2)
                    if stmt_result2.get("success"):
                        actions.append(
                            {
                                "tablespace": tablespace_name,
                                "action": "datafile_added",
                                "file": new_name,
                                "sql": add_sql2,
                            }
                        )
                    else:
                        actions.append(
                            {
                                "tablespace": tablespace_name,
                                "action": "add_datafile_failed",
                                "error": stmt_result2.get("error", "unknown"),
                                "sql": add_sql2,
                            }
                        )
                else:
                    actions.append(
                        {
                            "tablespace": tablespace_name,
                            "action": "add_datafile_failed",
                            "error": stmt_result.get("error", "unknown"),
                            "sql": add_sql,
                        }
                    )

        return actions

    @staticmethod
    def _derive_datafile_dir(datafiles: list[dict[str, Any]]) -> str:
        """Derive directory from existing datafiles for new file placement."""
        for df in datafiles:
            fname = df.get("FILE_NAME") or df.get("file_name", "")
            if fname:
                return os.path.dirname(fname)
        return ""

    # -- PostgreSQL checks ---------------------------------------------------

    def _check_postgresql(self) -> dict[str, Any]:
        ts_result = self.db_client.execute_query(_PG_TABLESPACE_SQL)
        db_result = self.db_client.execute_query(_PG_DATABASE_SIZE_SQL)
        tbl_result = self.db_client.execute_query(_PG_TABLE_SIZE_SQL)

        ts_data: list[dict[str, Any]] = []
        actions: list[dict[str, Any]] = []

        if "error" not in ts_result:
            ts_data.extend(ts_result.get("rows", []))
        if "error" not in db_result:
            ts_data.append({"_section": "databases", "rows": db_result.get("rows", [])})
        if "error" not in tbl_result:
            ts_data.append({"_section": "tables", "rows": tbl_result.get("rows", [])})

        for err_result in (ts_result, db_result, tbl_result):
            if "error" in err_result:
                actions.append(
                    {
                        "action": "query_error",
                        "error": err_result["error"],
                    }
                )

        status = "ok" if not actions else "warning"
        event = _event(status, ts_data, actions)
        self.events.append(event)
        return event
