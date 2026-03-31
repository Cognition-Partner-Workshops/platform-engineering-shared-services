"""Oracle alert log monitoring and error detection."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.alert_log_monitor")

# Oracle 19c supports V$DIAG_ALERT_EXT for programmatic access to alert log
SQL_ALERT_LOG_ENTRIES = """
SELECT
    originating_timestamp,
    message_text,
    message_level,
    component_id
FROM v$diag_alert_ext
WHERE originating_timestamp >= SYSTIMESTAMP - INTERVAL :hours HOUR
  AND message_text LIKE 'ORA-%'
ORDER BY originating_timestamp DESC
"""

SQL_ALERT_LOG_ALL = """
SELECT
    originating_timestamp,
    message_text,
    message_level,
    component_id
FROM v$diag_alert_ext
WHERE originating_timestamp >= SYSTIMESTAMP - INTERVAL :hours HOUR
ORDER BY originating_timestamp DESC
"""

# ORA error severity classification
CRITICAL_ERRORS = {
    "ORA-00600",  # Internal error
    "ORA-07445",  # Exception encountered (core dump)
    "ORA-04031",  # Unable to allocate shared memory
    "ORA-01578",  # Data block corruption
    "ORA-01110",  # Data file corruption
    "ORA-27157",  # OS post/wait facility error
    "ORA-16038",  # Log sequence cannot be archived
}

WARNING_ERRORS = {
    "ORA-01555",  # Snapshot too old
    "ORA-01652",  # Unable to extend temp segment
    "ORA-01653",  # Unable to extend table
    "ORA-01654",  # Unable to extend index
    "ORA-30036",  # Unable to extend undo segment
    "ORA-04036",  # PGA memory used by instance exceeds PGA_AGGREGATE_LIMIT
    "ORA-01691",  # Unable to extend lob segment
    "ORA-01688",  # Unable to extend table partition
}


@dataclass
class AlertLogEntry:
    timestamp: str
    message: str
    level: int
    component: str
    ora_code: str | None = None
    severity: str = "INFO"


@dataclass
class AlertLogSummary:
    total_entries: int = 0
    critical_count: int = 0
    warning_count: int = 0
    info_count: int = 0
    error_breakdown: dict[str, int] = field(default_factory=dict)
    entries: list[AlertLogEntry] = field(default_factory=list)


class AlertLogMonitor:
    """Monitors Oracle alert log for ORA- errors and anomalies."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter

    def _extract_ora_code(self, message: str) -> str | None:
        """Extract ORA-XXXXX error code from a message."""
        match = re.search(r"(ORA-\d{5})", message)
        return match.group(1) if match else None

    def _classify_severity(self, ora_code: str | None) -> str:
        """Classify the severity of an ORA error."""
        if ora_code is None:
            return "INFO"
        if ora_code in CRITICAL_ERRORS:
            return "CRITICAL"
        if ora_code in WARNING_ERRORS:
            return "WARNING"
        return "ERROR"

    def scan_alert_log(self, hours: int = 24, ora_only: bool = True) -> AlertLogSummary:
        """Scan the alert log for errors in the specified time window.

        Args:
            hours: Number of hours to look back.
            ora_only: If True, only return ORA- error entries.

        Returns:
            AlertLogSummary with categorized entries.
        """
        logger.info("Scanning alert log for the last %d hours...", hours)
        sql = SQL_ALERT_LOG_ENTRIES if ora_only else SQL_ALERT_LOG_ALL
        rows = self._conn.execute_query(sql, {"hours": hours})

        summary = AlertLogSummary()
        for row in rows:
            ora_code = self._extract_ora_code(str(row[1]))
            severity = self._classify_severity(ora_code)

            entry = AlertLogEntry(
                timestamp=str(row[0]),
                message=str(row[1])[:500],
                level=int(row[2]) if row[2] else 0,
                component=str(row[3]) if row[3] else "N/A",
                ora_code=ora_code,
                severity=severity,
            )
            summary.entries.append(entry)
            summary.total_entries += 1

            if severity == "CRITICAL":
                summary.critical_count += 1
            elif severity == "WARNING":
                summary.warning_count += 1
            else:
                summary.info_count += 1

            if ora_code:
                summary.error_breakdown[ora_code] = summary.error_breakdown.get(ora_code, 0) + 1

        logger.info(
            "Alert log scan complete: %d entries (%d critical, %d warning)",
            summary.total_entries, summary.critical_count, summary.warning_count,
        )
        return summary

    def monitor_and_alert(self, hours: int = 24) -> AlertLogSummary:
        """Scan alert log and send email if critical/warning errors found."""
        summary = self.scan_alert_log(hours=hours)

        if summary.critical_count > 0 or summary.warning_count > 0:
            self._send_alert_log_alert(summary, hours)

        return summary

    def _send_alert_log_alert(self, summary: AlertLogSummary, hours: int) -> None:
        """Send email alert for alert log issues."""
        # Error breakdown table
        breakdown_html = ""
        sorted_errors = sorted(summary.error_breakdown.items(), key=lambda x: x[1], reverse=True)
        for code, count in sorted_errors:
            severity = self._classify_severity(code)
            color = "#ff4444" if severity == "CRITICAL" else ("#ff8800" if severity == "WARNING" else "#333")
            breakdown_html += (
                f"<tr><td><code style='color:{color}'>{code}</code></td>"
                f"<td>{count}</td><td>{severity}</td></tr>"
            )

        # Recent critical entries
        critical_html = ""
        critical_entries = [e for e in summary.entries if e.severity == "CRITICAL"][:10]
        for entry in critical_entries:
            critical_html += (
                f"<tr><td>{entry.timestamp}</td>"
                f"<td><code>{entry.ora_code}</code></td>"
                f"<td>{entry.message[:200]}</td></tr>"
            )

        html = f"""
        <h3>Alert Log Summary (Last {hours} Hours)</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><td><strong>Total ORA- Entries</strong></td><td>{summary.total_entries}</td></tr>
            <tr><td><strong style="color:red">Critical</strong></td><td>{summary.critical_count}</td></tr>
            <tr><td><strong style="color:orange">Warning</strong></td><td>{summary.warning_count}</td></tr>
        </table>

        <h4>Error Breakdown:</h4>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>ORA Code</th><th>Count</th><th>Severity</th></tr>
            {breakdown_html}
        </table>
        """

        if critical_html:
            html += f"""
            <h4>Recent Critical Errors:</h4>
            <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>Timestamp</th><th>Code</th><th>Message</th></tr>
                {critical_html}
            </table>
            """

        subject_prefix = "CRITICAL" if summary.critical_count > 0 else "WARNING"
        self._alerter.send_alert(
            subject=f"{subject_prefix}: Alert Log Errors ({summary.total_entries} in last {hours}h)",
            body_html=html,
        )

    def get_report(self, hours: int = 24) -> str:
        """Generate a text report of alert log entries."""
        summary = self.scan_alert_log(hours=hours)

        lines = [
            "=" * 90,
            f"{'ALERT LOG REPORT (Last ' + str(hours) + ' Hours)':^90}",
            "=" * 90,
            f"  Total ORA- entries: {summary.total_entries}",
            f"  Critical:           {summary.critical_count}",
            f"  Warning:            {summary.warning_count}",
            "",
        ]

        if summary.error_breakdown:
            lines.append(f"{'Error Breakdown':^90}")
            lines.append("-" * 90)
            lines.append(f"  {'ORA Code':<15} {'Count':>8} {'Severity':<12}")
            lines.append("-" * 90)
            for code, count in sorted(summary.error_breakdown.items(), key=lambda x: x[1], reverse=True):
                severity = self._classify_severity(code)
                lines.append(f"  {code:<15} {count:>8} {severity:<12}")

        if summary.entries:
            lines.append("")
            lines.append(f"{'Recent Entries (last 20)':^90}")
            lines.append("-" * 90)
            for entry in summary.entries[:20]:
                lines.append(f"  [{entry.severity}] {entry.timestamp} - {entry.message[:80]}")

        lines.append("=" * 90)
        return "\n".join(lines)
