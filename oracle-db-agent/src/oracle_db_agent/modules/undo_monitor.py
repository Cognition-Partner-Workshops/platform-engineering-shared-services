"""Undo tablespace monitoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.undo_monitor")

SQL_UNDO_USAGE = """
SELECT
    tablespace_name,
    status,
    ROUND(SUM(bytes) / 1024 / 1024, 2) AS mb
FROM dba_undo_extents
GROUP BY tablespace_name, status
ORDER BY tablespace_name, status
"""

SQL_UNDO_TABLESPACE_SIZE = """
SELECT
    df.tablespace_name,
    ROUND(SUM(df.bytes) / 1024 / 1024, 2) AS total_mb,
    ROUND(SUM(df.bytes) / 1024 / 1024 - NVL(SUM(fs.free_bytes), 0) / 1024 / 1024, 2) AS used_mb,
    ROUND(NVL(SUM(fs.free_bytes), 0) / 1024 / 1024, 2) AS free_mb
FROM dba_data_files df
LEFT JOIN (
    SELECT tablespace_name, SUM(bytes) AS free_bytes
    FROM dba_free_space
    GROUP BY tablespace_name
) fs ON df.tablespace_name = fs.tablespace_name
WHERE df.tablespace_name = (SELECT value FROM v$parameter WHERE name = 'undo_tablespace')
GROUP BY df.tablespace_name
"""

SQL_UNDO_RETENTION = """
SELECT
    name,
    value
FROM v$parameter
WHERE name IN ('undo_tablespace', 'undo_retention', 'undo_management')
ORDER BY name
"""

SQL_UNDO_ACTIVE_TRANSACTIONS = """
SELECT
    s.sid,
    s.serial#,
    s.username,
    t.used_ublk * (SELECT value FROM v$parameter WHERE name = 'db_block_size') / 1024 / 1024 AS undo_mb,
    t.status,
    s.program,
    s.machine
FROM v$transaction t
JOIN v$session s ON t.ses_addr = s.saddr
ORDER BY t.used_ublk DESC
"""

SQL_ORA_01555_CHECK = """
SELECT
    end_time,
    unxpstealcnt AS unexpired_stolen,
    expstealcnt AS expired_stolen,
    ssolderrcnt AS snapshot_too_old_count
FROM v$undostat
WHERE end_time >= SYSDATE - 1
  AND ssolderrcnt > 0
ORDER BY end_time DESC
"""


@dataclass
class UndoExtentInfo:
    tablespace_name: str
    status: str
    mb: float


@dataclass
class UndoTablespaceInfo:
    tablespace_name: str
    total_mb: float
    used_mb: float
    free_mb: float
    pct_used: float


class UndoMonitor:
    """Monitors undo tablespace usage and ORA-01555 risk."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter
        self._threshold = config.thresholds.undo_warning_pct

    def check_undo_usage(self) -> list[UndoExtentInfo]:
        """Check undo extent usage by status."""
        logger.info("Checking undo tablespace usage...")
        rows = self._conn.execute_query(SQL_UNDO_USAGE)
        results: list[UndoExtentInfo] = []
        for row in rows:
            info = UndoExtentInfo(
                tablespace_name=row[0],
                status=row[1],
                mb=float(row[2]),
            )
            results.append(info)
        return results

    def check_undo_tablespace(self) -> UndoTablespaceInfo:
        """Check overall undo tablespace size and usage."""
        rows = self._conn.execute_query(SQL_UNDO_TABLESPACE_SIZE)
        if rows:
            row = rows[0]
            total_mb = float(row[1])
            used_mb = float(row[2])
            free_mb = float(row[3])
            pct_used = (used_mb / total_mb * 100) if total_mb > 0 else 0.0
            info = UndoTablespaceInfo(
                tablespace_name=row[0],
                total_mb=total_mb,
                used_mb=used_mb,
                free_mb=free_mb,
                pct_used=pct_used,
            )
            if pct_used >= self._threshold:
                logger.warning(
                    "Undo tablespace %s is %.1f%% full (%.1f MB free)",
                    info.tablespace_name, pct_used, free_mb,
                )
            return info
        return UndoTablespaceInfo("UNKNOWN", 0, 0, 0, 0)

    def check_ora_01555_risk(self) -> bool:
        """Check if ORA-01555 (snapshot too old) errors occurred recently."""
        rows = self._conn.execute_query(SQL_ORA_01555_CHECK)
        if rows:
            total_errors = sum(int(r[3]) for r in rows)
            if total_errors > 0:
                logger.warning(
                    "ORA-01555 risk detected: %d snapshot-too-old errors in last 24 hours.",
                    total_errors,
                )
                return True
        return False

    def monitor_and_alert(self) -> UndoTablespaceInfo:
        """Full undo monitoring with alerting."""
        ts_info = self.check_undo_tablespace()
        extents = self.check_undo_usage()
        ora_01555 = self.check_ora_01555_risk()

        if ts_info.pct_used >= self._threshold or ora_01555:
            self._send_undo_alert(ts_info, extents, ora_01555)

        return ts_info

    def _send_undo_alert(
        self,
        ts_info: UndoTablespaceInfo,
        extents: list[UndoExtentInfo],
        ora_01555: bool,
    ) -> None:
        """Send email alert for undo issues."""
        extent_html = ""
        for e in extents:
            extent_html += (
                f"<tr><td>{e.tablespace_name}</td><td>{e.status}</td>"
                f"<td>{e.mb:.1f}</td></tr>"
            )

        ora_warning = ""
        if ora_01555:
            ora_warning = (
                '<p style="color:red;font-weight:bold">'
                "WARNING: ORA-01555 (snapshot too old) errors detected in the last 24 hours. "
                "Consider increasing undo_retention or undo tablespace size.</p>"
            )

        html = f"""
        <h3>Undo Tablespace Alert</h3>
        {ora_warning}
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><td><strong>Tablespace</strong></td><td>{ts_info.tablespace_name}</td></tr>
            <tr><td><strong>Used</strong></td><td>{ts_info.pct_used:.1f}% ({ts_info.used_mb:.1f} MB)</td></tr>
            <tr><td><strong>Free</strong></td><td>{ts_info.free_mb:.1f} MB</td></tr>
            <tr><td><strong>Total</strong></td><td>{ts_info.total_mb:.1f} MB</td></tr>
        </table>
        <h4>Undo Extent Breakdown:</h4>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Tablespace</th><th>Status</th><th>MB</th></tr>
            {extent_html}
        </table>
        """
        self._alerter.send_alert(subject="Undo Tablespace Alert", body_html=html)

    def get_report(self) -> str:
        """Generate a text report of undo usage."""
        ts_info = self.check_undo_tablespace()
        extents = self.check_undo_usage()
        ora_01555 = self.check_ora_01555_risk()

        lines = [
            "=" * 70,
            f"{'UNDO TABLESPACE REPORT':^70}",
            "=" * 70,
            f"  Tablespace:    {ts_info.tablespace_name}",
            f"  Total:         {ts_info.total_mb:.1f} MB",
            f"  Used:          {ts_info.used_mb:.1f} MB ({ts_info.pct_used:.1f}%)",
            f"  Free:          {ts_info.free_mb:.1f} MB",
            f"  ORA-01555:     {'YES - RISK DETECTED' if ora_01555 else 'No recent errors'}",
            "",
            f"{'Extent Breakdown':^70}",
            "-" * 70,
            f"  {'Status':<20} {'MB':>10}",
            "-" * 70,
        ]
        for e in extents:
            lines.append(f"  {e.status:<20} {e.mb:>9.1f}")
        lines.append("=" * 70)
        return "\n".join(lines)
