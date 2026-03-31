"""Long-running query and session monitoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.approval import ApprovalManager
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.session_monitor")

SQL_LONG_RUNNING_QUERIES = """
SELECT
    s.sid,
    s.serial#,
    s.username,
    s.machine,
    s.program,
    s.status,
    s.logon_time,
    ROUND(q.elapsed_time / 1000000, 1) AS elapsed_sec,
    q.sql_id,
    SUBSTR(q.sql_text, 1, 200) AS sql_text
FROM v$session s
JOIN v$sql q ON s.sql_id = q.sql_id AND s.sql_child_number = q.child_number
WHERE s.username IS NOT NULL
  AND s.status = 'ACTIVE'
  AND q.elapsed_time > :threshold_us
ORDER BY q.elapsed_time DESC
"""

SQL_ACTIVE_SESSIONS_SUMMARY = """
SELECT
    username,
    machine,
    program,
    status,
    COUNT(*) AS session_count
FROM v$session
WHERE username IS NOT NULL
GROUP BY username, machine, program, status
ORDER BY session_count DESC
"""

SQL_BLOCKING_SESSIONS = """
SELECT
    blocker.sid AS blocker_sid,
    blocker.serial# AS blocker_serial,
    blocker.username AS blocker_user,
    blocker.machine AS blocker_machine,
    waiter.sid AS waiter_sid,
    waiter.serial# AS waiter_serial,
    waiter.username AS waiter_user,
    waiter.event AS wait_event,
    waiter.seconds_in_wait
FROM v$session waiter
JOIN v$session blocker ON waiter.blocking_session = blocker.sid
WHERE waiter.blocking_session IS NOT NULL
ORDER BY waiter.seconds_in_wait DESC
"""

SQL_KILL_SESSION = "ALTER SYSTEM KILL SESSION '{sid},{serial}' IMMEDIATE"


@dataclass
class LongRunningQuery:
    sid: int
    serial: int
    username: str
    machine: str
    program: str
    status: str
    logon_time: str | None
    elapsed_sec: float
    sql_id: str
    sql_text: str


@dataclass
class BlockingInfo:
    blocker_sid: int
    blocker_serial: int
    blocker_user: str
    blocker_machine: str
    waiter_sid: int
    waiter_serial: int
    waiter_user: str
    wait_event: str
    seconds_in_wait: int


class SessionMonitor:
    """Monitors long-running queries, active sessions, and blocking locks."""

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
        self._threshold_sec = config.thresholds.long_query_seconds

    def find_long_running_queries(self) -> list[LongRunningQuery]:
        """Find active queries running longer than the configured threshold."""
        logger.info("Scanning for long-running queries (threshold: %ds)...", self._threshold_sec)
        threshold_us = self._threshold_sec * 1_000_000
        rows = self._conn.execute_query(
            SQL_LONG_RUNNING_QUERIES, {"threshold_us": threshold_us}
        )
        results: list[LongRunningQuery] = []
        for row in rows:
            q = LongRunningQuery(
                sid=int(row[0]),
                serial=int(row[1]),
                username=row[2] or "N/A",
                machine=row[3] or "N/A",
                program=row[4] or "N/A",
                status=row[5] or "N/A",
                logon_time=str(row[6]) if row[6] else None,
                elapsed_sec=float(row[7]),
                sql_id=row[8] or "N/A",
                sql_text=row[9] or "N/A",
            )
            results.append(q)

        if results:
            logger.warning("Found %d long-running queries.", len(results))
        else:
            logger.info("No long-running queries found.")
        return results

    def find_blocking_sessions(self) -> list[BlockingInfo]:
        """Find sessions that are blocking other sessions."""
        logger.info("Checking for blocking sessions...")
        rows = self._conn.execute_query(SQL_BLOCKING_SESSIONS)
        results: list[BlockingInfo] = []
        for row in rows:
            info = BlockingInfo(
                blocker_sid=int(row[0]),
                blocker_serial=int(row[1]),
                blocker_user=row[2] or "N/A",
                blocker_machine=row[3] or "N/A",
                waiter_sid=int(row[4]),
                waiter_serial=int(row[5]),
                waiter_user=row[6] or "N/A",
                wait_event=row[7] or "N/A",
                seconds_in_wait=int(row[8]) if row[8] else 0,
            )
            results.append(info)

        if results:
            logger.warning("Found %d blocking lock chains.", len(results))
        return results

    def request_kill_session(self, sid: int, serial: int, reason: str) -> str:
        """Request approval to kill a session (destructive action)."""
        sql = SQL_KILL_SESSION.format(sid=sid, serial=serial)

        if self._approval.requires_approval("kill_session"):
            pending = self._approval.request_approval(
                action="kill_session",
                description=f"Kill session {sid},{serial}: {reason}",
                sql_or_command=sql,
            )
            return f"Approval requested: {pending.approval_id} (kill session {sid},{serial})"
        else:
            self._conn.execute_ddl(sql)
            return f"Session {sid},{serial} killed."

    def monitor_and_alert(self) -> list[LongRunningQuery]:
        """Check for long-running queries and blocking sessions, send alerts."""
        long_queries = self.find_long_running_queries()
        blocking = self.find_blocking_sessions()

        if long_queries or blocking:
            self._send_session_alert(long_queries, blocking)

        return long_queries

    def _send_session_alert(
        self,
        long_queries: list[LongRunningQuery],
        blocking: list[BlockingInfo],
    ) -> None:
        """Send email alert for session issues."""
        query_html = ""
        for q in long_queries[:20]:
            elapsed_str = f"{q.elapsed_sec:.0f}s"
            if q.elapsed_sec > 3600:
                elapsed_str = f"{q.elapsed_sec / 3600:.1f}h"
            query_html += (
                f"<tr><td>{q.sid},{q.serial}</td><td>{q.username}</td>"
                f"<td>{q.machine}</td><td>{elapsed_str}</td>"
                f"<td><code>{q.sql_id}</code></td>"
                f"<td><small>{q.sql_text[:100]}</small></td></tr>"
            )

        blocking_html = ""
        for b in blocking[:20]:
            blocking_html += (
                f"<tr><td>{b.blocker_sid},{b.blocker_serial}</td>"
                f"<td>{b.blocker_user}</td>"
                f"<td>{b.waiter_sid},{b.waiter_serial}</td>"
                f"<td>{b.waiter_user}</td>"
                f"<td>{b.wait_event}</td>"
                f"<td>{b.seconds_in_wait}s</td></tr>"
            )

        html = ""
        if long_queries:
            html += f"""
            <h3>Long-Running Queries ({len(long_queries)} found)</h3>
            <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>SID,Serial#</th><th>User</th><th>Machine</th>
                <th>Elapsed</th><th>SQL ID</th><th>SQL Text</th></tr>
                {query_html}
            </table>
            """
        if blocking:
            html += f"""
            <h3>Blocking Sessions ({len(blocking)} chains)</h3>
            <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>Blocker</th><th>Blocker User</th><th>Waiter</th>
                <th>Waiter User</th><th>Wait Event</th><th>Wait Time</th></tr>
                {blocking_html}
            </table>
            """

        if html:
            self._alerter.send_alert(
                subject=f"Session Alert: {len(long_queries)} long queries, {len(blocking)} blocking chains",
                body_html=html,
            )

    def get_report(self) -> str:
        """Generate a text report of session status."""
        long_queries = self.find_long_running_queries()
        blocking = self.find_blocking_sessions()

        lines = [
            "=" * 100,
            f"{'SESSION MONITOR REPORT':^100}",
            "=" * 100,
        ]

        if long_queries:
            lines.append(f"\nLong-Running Queries ({len(long_queries)}):")
            lines.append("-" * 100)
            lines.append(f"{'SID,Serial':<15} {'User':<15} {'Machine':<20} {'Elapsed':>10} {'SQL ID':<15}")
            lines.append("-" * 100)
            for q in long_queries:
                elapsed_str = f"{q.elapsed_sec:.0f}s"
                lines.append(
                    f"{q.sid},{q.serial:<10} {q.username:<15} {q.machine:<20} "
                    f"{elapsed_str:>10} {q.sql_id:<15}"
                )
        else:
            lines.append("\nNo long-running queries found.")

        if blocking:
            lines.append(f"\nBlocking Sessions ({len(blocking)}):")
            lines.append("-" * 100)
            for b in blocking:
                lines.append(
                    f"  Blocker: {b.blocker_sid},{b.blocker_serial} ({b.blocker_user}) "
                    f"-> Waiter: {b.waiter_sid},{b.waiter_serial} ({b.waiter_user}) "
                    f"waiting {b.seconds_in_wait}s"
                )

        lines.append("=" * 100)
        return "\n".join(lines)
