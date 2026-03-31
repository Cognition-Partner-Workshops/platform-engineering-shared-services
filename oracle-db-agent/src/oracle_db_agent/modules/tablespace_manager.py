"""Tablespace monitoring and auto-extend management."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.approval import ApprovalManager
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.tablespace_manager")

# ---------------------------------------------------------------------------
# SQL Queries
# ---------------------------------------------------------------------------

SQL_TABLESPACE_USAGE = """
SELECT
    df.tablespace_name,
    ROUND(df.total_bytes / 1024 / 1024, 2) AS total_mb,
    ROUND(NVL(fs.free_bytes, 0) / 1024 / 1024, 2) AS free_mb,
    ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / 1024 / 1024, 2) AS used_mb,
    ROUND((df.total_bytes - NVL(fs.free_bytes, 0)) / df.total_bytes * 100, 2) AS pct_used,
    df.datafile_count,
    NVL(ae.autoextend_count, 0) AS autoextend_count
FROM
    (SELECT tablespace_name,
            SUM(bytes) AS total_bytes,
            COUNT(*) AS datafile_count
     FROM dba_data_files
     GROUP BY tablespace_name) df
LEFT JOIN
    (SELECT tablespace_name, SUM(bytes) AS free_bytes
     FROM dba_free_space
     GROUP BY tablespace_name) fs
    ON df.tablespace_name = fs.tablespace_name
LEFT JOIN
    (SELECT tablespace_name, COUNT(*) AS autoextend_count
     FROM dba_data_files
     WHERE autoextensible = 'YES'
     GROUP BY tablespace_name) ae
    ON df.tablespace_name = ae.tablespace_name
ORDER BY pct_used DESC
"""

SQL_DATAFILE_DETAILS = """
SELECT
    file_name,
    tablespace_name,
    ROUND(bytes / 1024 / 1024, 2) AS size_mb,
    ROUND(maxbytes / 1024 / 1024, 2) AS max_size_mb,
    autoextensible,
    ROUND(increment_by * (SELECT value FROM v$parameter WHERE name = 'db_block_size') / 1024 / 1024, 2) AS increment_mb
FROM dba_data_files
WHERE tablespace_name = :ts_name
ORDER BY file_id
"""

SQL_NEXT_DATAFILE_ID = """
SELECT NVL(MAX(file_id), 0) + 1 AS next_id FROM dba_data_files
"""


@dataclass
class TablespaceInfo:
    """Tablespace usage information."""

    name: str
    total_mb: float
    free_mb: float
    used_mb: float
    pct_used: float
    datafile_count: int
    autoextend_count: int

    @property
    def status(self) -> str:
        if self.pct_used >= 90:
            return "CRITICAL"
        if self.pct_used >= 80:
            return "WARNING"
        return "OK"


class TablespaceManager:
    """Monitors tablespace usage and auto-extends/creates datafiles."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
        approval_mgr: ApprovalManager,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter
        self._approval = approval_mgr
        self._ts_config = config.tablespace_management
        self._thresholds = config.thresholds

    def check_tablespace_usage(self) -> list[TablespaceInfo]:
        """Query and return tablespace usage for all tablespaces."""
        logger.info("Checking tablespace usage...")
        rows = self._conn.execute_query(SQL_TABLESPACE_USAGE)
        results: list[TablespaceInfo] = []
        for row in rows:
            ts = TablespaceInfo(
                name=row[0],
                total_mb=float(row[1]),
                free_mb=float(row[2]),
                used_mb=float(row[3]),
                pct_used=float(row[4]),
                datafile_count=int(row[5]),
                autoextend_count=int(row[6]),
            )
            results.append(ts)
            if ts.pct_used >= self._thresholds.tablespace_critical_pct:
                logger.critical(
                    "CRITICAL: Tablespace %s is %.1f%% full (%.1f MB free)",
                    ts.name, ts.pct_used, ts.free_mb,
                )
            elif ts.pct_used >= self._thresholds.tablespace_warning_pct:
                logger.warning(
                    "WARNING: Tablespace %s is %.1f%% full (%.1f MB free)",
                    ts.name, ts.pct_used, ts.free_mb,
                )
            else:
                logger.info(
                    "OK: Tablespace %s is %.1f%% full (%.1f MB free)",
                    ts.name, ts.pct_used, ts.free_mb,
                )
        return results

    def auto_manage(self) -> list[str]:
        """Check tablespace usage and auto-extend or create datafiles as needed.

        Returns a list of actions taken.
        """
        actions_taken: list[str] = []
        tablespaces = self.check_tablespace_usage()

        for ts in tablespaces:
            if ts.name in self._ts_config.exclude_tablespaces:
                continue

            if ts.pct_used < self._thresholds.tablespace_critical_pct:
                continue

            # Try to enable autoextend on existing datafiles first
            if ts.autoextend_count < ts.datafile_count:
                action = self._enable_autoextend(ts.name)
                if action:
                    actions_taken.append(action)
                    continue

            # If still critical, add a new datafile
            if self._ts_config.auto_extend:
                action = self._add_datafile(ts.name)
                if action:
                    actions_taken.append(action)

        # Send alert if any tablespace is in warning/critical state
        alerts = [ts for ts in tablespaces if ts.pct_used >= self._thresholds.tablespace_warning_pct]
        if alerts:
            self._send_tablespace_alert(alerts, actions_taken)

        return actions_taken

    def _enable_autoextend(self, tablespace_name: str) -> str | None:
        """Enable autoextend on datafiles that don't have it enabled."""
        rows = self._conn.execute_query(
            SQL_DATAFILE_DETAILS, {"ts_name": tablespace_name}
        )
        for row in rows:
            file_name = row[0]
            autoextensible = row[4]
            if autoextensible == "NO":
                sql = (
                    f"ALTER DATABASE DATAFILE '{file_name}' "
                    f"AUTOEXTEND ON NEXT {self._ts_config.extend_size_mb}M "
                    f"MAXSIZE {self._ts_config.max_datafile_size_gb}G"
                )
                try:
                    self._conn.execute_ddl(sql)
                    msg = (
                        f"Enabled autoextend on {file_name} "
                        f"(next={self._ts_config.extend_size_mb}M, "
                        f"max={self._ts_config.max_datafile_size_gb}G)"
                    )
                    logger.info(msg)
                    return msg
                except Exception as exc:
                    logger.error("Failed to enable autoextend on %s: %s", file_name, exc)
        return None

    def _add_datafile(self, tablespace_name: str) -> str | None:
        """Add a new datafile to the tablespace."""
        # Determine next datafile name
        rows = self._conn.execute_query(SQL_NEXT_DATAFILE_ID)
        next_id = int(rows[0][0]) if rows else 999
        datafile_path = os.path.join(
            self._ts_config.datafile_path,
            f"{tablespace_name.lower()}_{next_id:03d}.dbf",
        )
        sql = (
            f"ALTER TABLESPACE {tablespace_name} ADD DATAFILE "
            f"'{datafile_path}' SIZE {self._ts_config.extend_size_mb}M "
            f"AUTOEXTEND ON NEXT {self._ts_config.extend_size_mb}M "
            f"MAXSIZE {self._ts_config.max_datafile_size_gb}G"
        )

        try:
            self._conn.execute_ddl(sql)
            msg = f"Added datafile {datafile_path} to tablespace {tablespace_name}"
            logger.info(msg)
            return msg
        except Exception as exc:
            logger.error("Failed to add datafile to %s: %s", tablespace_name, exc)
            return None

    def _send_tablespace_alert(
        self, alerts: list[TablespaceInfo], actions: list[str]
    ) -> None:
        """Send email alert for tablespace issues."""
        rows_html = ""
        for ts in alerts:
            color = "#ff4444" if ts.status == "CRITICAL" else "#ff8800"
            rows_html += (
                f"<tr>"
                f"<td>{ts.name}</td>"
                f"<td style='color:{color};font-weight:bold'>{ts.pct_used:.1f}%</td>"
                f"<td>{ts.used_mb:.1f} MB</td>"
                f"<td>{ts.free_mb:.1f} MB</td>"
                f"<td>{ts.total_mb:.1f} MB</td>"
                f"<td>{ts.datafile_count}</td>"
                f"</tr>"
            )

        actions_html = ""
        if actions:
            actions_html = "<h4>Actions Taken:</h4><ul>"
            for action in actions:
                actions_html += f"<li>{action}</li>"
            actions_html += "</ul>"

        html = f"""
        <h3>Tablespace Usage Alert</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Tablespace</th><th>Used %</th><th>Used MB</th>
            <th>Free MB</th><th>Total MB</th><th>Datafiles</th></tr>
            {rows_html}
        </table>
        {actions_html}
        """
        self._alerter.send_alert(
            subject="Tablespace Usage Alert",
            body_html=html,
        )

    def get_report(self) -> str:
        """Generate a text report of tablespace usage."""
        tablespaces = self.check_tablespace_usage()
        lines = [
            "=" * 90,
            f"{'TABLESPACE USAGE REPORT':^90}",
            "=" * 90,
            f"{'Tablespace':<25} {'Used %':>8} {'Used MB':>12} {'Free MB':>12} {'Total MB':>12} {'Status':>10}",
            "-" * 90,
        ]
        for ts in tablespaces:
            lines.append(
                f"{ts.name:<25} {ts.pct_used:>7.1f}% {ts.used_mb:>11.1f} {ts.free_mb:>11.1f} "
                f"{ts.total_mb:>11.1f} {ts.status:>10}"
            )
        lines.append("=" * 90)
        return "\n".join(lines)
