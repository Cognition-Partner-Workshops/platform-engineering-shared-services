"""Temp tablespace monitoring and cleanup."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.temp_monitor")

SQL_TEMP_USAGE = """
SELECT
    tsh.tablespace_name,
    ROUND(SUM(tsh.bytes_used) / 1024 / 1024, 2) AS used_mb,
    ROUND(SUM(tsh.bytes_free) / 1024 / 1024, 2) AS free_mb,
    ROUND((SUM(tsh.bytes_used) + SUM(tsh.bytes_free)) / 1024 / 1024, 2) AS total_mb,
    ROUND(SUM(tsh.bytes_used) / (SUM(tsh.bytes_used) + SUM(tsh.bytes_free)) * 100, 2) AS pct_used
FROM v$temp_space_header tsh
GROUP BY tsh.tablespace_name
ORDER BY pct_used DESC
"""

SQL_TEMP_CONSUMERS = """
SELECT
    s.sid,
    s.serial#,
    s.username,
    s.machine,
    s.program,
    ROUND(su.blocks * (SELECT value FROM v$parameter WHERE name = 'db_block_size') / 1024 / 1024, 2) AS temp_mb,
    su.tablespace
FROM v$sort_usage su
JOIN v$session s ON su.session_addr = s.saddr
ORDER BY su.blocks DESC
"""

SQL_TEMP_FILE_DETAILS = """
SELECT
    file_name,
    tablespace_name,
    ROUND(bytes / 1024 / 1024, 2) AS size_mb,
    ROUND(maxbytes / 1024 / 1024, 2) AS max_size_mb,
    autoextensible
FROM dba_temp_files
ORDER BY tablespace_name, file_name
"""


@dataclass
class TempUsageInfo:
    tablespace_name: str
    used_mb: float
    free_mb: float
    total_mb: float
    pct_used: float


@dataclass
class TempConsumer:
    sid: int
    serial: int
    username: str
    machine: str
    program: str
    temp_mb: float
    tablespace: str


class TempMonitor:
    """Monitors temp tablespace usage and identifies heavy consumers."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter
        self._threshold = config.thresholds.temp_warning_pct

    def check_temp_usage(self) -> list[TempUsageInfo]:
        """Check temp tablespace usage."""
        logger.info("Checking temp tablespace usage...")
        rows = self._conn.execute_query(SQL_TEMP_USAGE)
        results: list[TempUsageInfo] = []
        for row in rows:
            info = TempUsageInfo(
                tablespace_name=row[0],
                used_mb=float(row[1]),
                free_mb=float(row[2]),
                total_mb=float(row[3]),
                pct_used=float(row[4]),
            )
            results.append(info)
            if info.pct_used >= self._threshold:
                logger.warning(
                    "Temp tablespace %s is %.1f%% full (%.1f MB used / %.1f MB total)",
                    info.tablespace_name, info.pct_used, info.used_mb, info.total_mb,
                )
            else:
                logger.info(
                    "Temp tablespace %s: %.1f%% used (%.1f MB free)",
                    info.tablespace_name, info.pct_used, info.free_mb,
                )
        return results

    def get_temp_consumers(self) -> list[TempConsumer]:
        """Identify sessions consuming temp space."""
        logger.info("Identifying temp space consumers...")
        rows = self._conn.execute_query(SQL_TEMP_CONSUMERS)
        results: list[TempConsumer] = []
        for row in rows:
            consumer = TempConsumer(
                sid=int(row[0]),
                serial=int(row[1]),
                username=row[2] or "N/A",
                machine=row[3] or "N/A",
                program=row[4] or "N/A",
                temp_mb=float(row[5]),
                tablespace=row[6],
            )
            results.append(consumer)
        if results:
            logger.info("Found %d sessions consuming temp space.", len(results))
        return results

    def monitor_and_alert(self) -> list[TempUsageInfo]:
        """Check temp usage and alert if thresholds are exceeded."""
        usage = self.check_temp_usage()
        alerts = [u for u in usage if u.pct_used >= self._threshold]

        if alerts:
            consumers = self.get_temp_consumers()
            self._send_temp_alert(alerts, consumers)

        return usage

    def _send_temp_alert(
        self, alerts: list[TempUsageInfo], consumers: list[TempConsumer]
    ) -> None:
        """Send email alert for temp tablespace issues."""
        usage_html = ""
        for u in alerts:
            color = "#ff4444" if u.pct_used >= 90 else "#ff8800"
            usage_html += (
                f"<tr><td>{u.tablespace_name}</td>"
                f"<td style='color:{color};font-weight:bold'>{u.pct_used:.1f}%</td>"
                f"<td>{u.used_mb:.1f}</td><td>{u.free_mb:.1f}</td>"
                f"<td>{u.total_mb:.1f}</td></tr>"
            )

        consumer_html = ""
        for c in consumers[:20]:
            consumer_html += (
                f"<tr><td>{c.sid},{c.serial}</td><td>{c.username}</td>"
                f"<td>{c.machine}</td><td>{c.program}</td>"
                f"<td>{c.temp_mb:.1f}</td></tr>"
            )

        html = f"""
        <h3>Temp Tablespace Alert</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Tablespace</th><th>Used %</th><th>Used MB</th><th>Free MB</th><th>Total MB</th></tr>
            {usage_html}
        </table>
        <h4>Top Temp Space Consumers:</h4>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>SID,Serial#</th><th>User</th><th>Machine</th><th>Program</th><th>Temp MB</th></tr>
            {consumer_html}
        </table>
        """
        self._alerter.send_alert(subject="Temp Tablespace Usage Alert", body_html=html)

    def get_report(self) -> str:
        """Generate a text report of temp usage."""
        usage = self.check_temp_usage()
        consumers = self.get_temp_consumers()

        lines = [
            "=" * 80,
            f"{'TEMP TABLESPACE USAGE REPORT':^80}",
            "=" * 80,
            f"{'Tablespace':<20} {'Used %':>8} {'Used MB':>10} {'Free MB':>10} {'Total MB':>10}",
            "-" * 80,
        ]
        for u in usage:
            lines.append(
                f"{u.tablespace_name:<20} {u.pct_used:>7.1f}% {u.used_mb:>9.1f} "
                f"{u.free_mb:>9.1f} {u.total_mb:>9.1f}"
            )

        if consumers:
            lines.append("")
            lines.append(f"{'TOP TEMP CONSUMERS':^80}")
            lines.append("-" * 80)
            lines.append(f"{'SID,Serial#':<15} {'User':<15} {'Machine':<20} {'Temp MB':>10}")
            lines.append("-" * 80)
            for c in consumers[:20]:
                lines.append(
                    f"{c.sid},{c.serial:<10} {c.username:<15} {c.machine:<20} {c.temp_mb:>9.1f}"
                )

        lines.append("=" * 80)
        return "\n".join(lines)
