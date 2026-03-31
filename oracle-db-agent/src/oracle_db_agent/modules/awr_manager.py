"""AWR/ADDM Snapshot and Report generation for performance testing."""

from __future__ import annotations

import logging
import os
from datetime import datetime

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.awr_manager")

SQL_CREATE_SNAPSHOT = """
BEGIN
    DBMS_WORKLOAD_REPOSITORY.CREATE_SNAPSHOT();
END;
"""

SQL_LATEST_SNAPSHOT = """
SELECT snap_id, begin_interval_time, end_interval_time
FROM dba_hist_snapshot
WHERE instance_number = (SELECT instance_number FROM v$instance)
ORDER BY snap_id DESC
FETCH FIRST 1 ROWS ONLY
"""

SQL_GET_DBID = """
SELECT dbid FROM v$database
"""

SQL_GET_INSTANCE_NUMBER = """
SELECT instance_number FROM v$instance
"""

SQL_AWR_REPORT_HTML = """
SELECT output
FROM TABLE(DBMS_WORKLOAD_REPOSITORY.AWR_REPORT_HTML(
    l_dbid     => :dbid,
    l_inst_num => :inst_num,
    l_bid      => :begin_snap,
    l_eid      => :end_snap
))
"""

SQL_ADDM_REPORT = """
DECLARE
    v_task_name VARCHAR2(100);
    v_task_desc VARCHAR2(256);
    v_id        NUMBER;
BEGIN
    v_task_name := :task_name;
    v_task_desc := 'ADDM report for perf test window';
    DBMS_ADVISOR.CREATE_TASK('ADDM', v_id, v_task_name, v_task_desc);
    DBMS_ADVISOR.SET_TASK_PARAMETER(v_task_name, 'START_SNAPSHOT', :begin_snap);
    DBMS_ADVISOR.SET_TASK_PARAMETER(v_task_name, 'END_SNAPSHOT', :end_snap);
    DBMS_ADVISOR.SET_TASK_PARAMETER(v_task_name, 'INSTANCE', :inst_num);
    DBMS_ADVISOR.SET_TASK_PARAMETER(v_task_name, 'DB_ID', :dbid);
    DBMS_ADVISOR.EXECUTE_TASK(v_task_name);
END;
"""

SQL_GET_ADDM_REPORT = """
SELECT DBMS_ADVISOR.GET_TASK_REPORT(:task_name, 'TEXT', 'ALL') FROM DUAL
"""

SQL_SNAPSHOT_RANGE = """
SELECT MIN(snap_id) AS min_snap, MAX(snap_id) AS max_snap,
       MIN(begin_interval_time) AS min_time, MAX(end_interval_time) AS max_time
FROM dba_hist_snapshot
WHERE instance_number = (SELECT instance_number FROM v$instance)
  AND begin_interval_time >= SYSTIMESTAMP - INTERVAL :hours HOUR
"""


class AWRManager:
    """Manages AWR snapshots and report generation for performance testing."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter
        self._report_dir = config.performance_testing.awr_report_dir

    def _get_dbid(self) -> int:
        rows = self._conn.execute_query(SQL_GET_DBID)
        return int(rows[0][0])

    def _get_instance_number(self) -> int:
        rows = self._conn.execute_query(SQL_GET_INSTANCE_NUMBER)
        return int(rows[0][0])

    def create_snapshot(self) -> int | None:
        """Create an AWR snapshot and return the snapshot ID."""
        try:
            logger.info("Creating AWR snapshot...")
            self._conn.execute_plsql(SQL_CREATE_SNAPSHOT)
            rows = self._conn.execute_query(SQL_LATEST_SNAPSHOT)
            if rows:
                snap_id = int(rows[0][0])
                snap_time = rows[0][2]
                logger.info("AWR snapshot created: snap_id=%d, time=%s", snap_id, snap_time)
                return snap_id
            logger.warning("Snapshot created but could not retrieve snap_id.")
            return None
        except Exception as exc:
            logger.error("Failed to create AWR snapshot: %s", exc)
            return None

    def get_latest_snapshot_id(self) -> int | None:
        """Get the most recent AWR snapshot ID."""
        rows = self._conn.execute_query(SQL_LATEST_SNAPSHOT)
        if rows:
            return int(rows[0][0])
        return None

    def generate_awr_report(
        self, begin_snap: int, end_snap: int, output_file: str | None = None
    ) -> str | None:
        """Generate an AWR HTML report between two snapshots.

        Returns the path to the generated report file.
        """
        try:
            dbid = self._get_dbid()
            inst_num = self._get_instance_number()

            logger.info(
                "Generating AWR report: dbid=%d, inst=%d, snaps=%d-%d",
                dbid, inst_num, begin_snap, end_snap,
            )

            rows = self._conn.execute_query(
                SQL_AWR_REPORT_HTML,
                {
                    "dbid": dbid,
                    "inst_num": inst_num,
                    "begin_snap": begin_snap,
                    "end_snap": end_snap,
                },
            )

            html_content = "\n".join(str(row[0]) for row in rows if row[0])

            if not output_file:
                os.makedirs(self._report_dir, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = os.path.join(
                    self._report_dir,
                    f"awr_report_{begin_snap}_{end_snap}_{timestamp}.html",
                )

            with open(output_file, "w", encoding="utf-8") as f:
                f.write(html_content)

            logger.info("AWR report saved to: %s", output_file)
            return output_file
        except Exception as exc:
            logger.error("Failed to generate AWR report: %s", exc)
            return None

    def generate_addm_report(
        self, begin_snap: int, end_snap: int, output_file: str | None = None
    ) -> str | None:
        """Generate an ADDM report between two snapshots.

        Returns the path to the generated report file.
        """
        try:
            dbid = self._get_dbid()
            inst_num = self._get_instance_number()
            task_name = f"ADDM_{begin_snap}_{end_snap}_{datetime.now().strftime('%H%M%S')}"

            logger.info("Running ADDM analysis: task=%s, snaps=%d-%d", task_name, begin_snap, end_snap)

            self._conn.execute_plsql(
                SQL_ADDM_REPORT,
                {
                    "task_name": task_name,
                    "begin_snap": begin_snap,
                    "end_snap": end_snap,
                    "inst_num": inst_num,
                    "dbid": dbid,
                },
            )

            rows = self._conn.execute_query(
                SQL_GET_ADDM_REPORT, {"task_name": task_name}
            )
            report_text = str(rows[0][0]) if rows else "No ADDM report generated."

            if not output_file:
                os.makedirs(self._report_dir, exist_ok=True)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                output_file = os.path.join(
                    self._report_dir,
                    f"addm_report_{begin_snap}_{end_snap}_{timestamp}.txt",
                )

            with open(output_file, "w", encoding="utf-8") as f:
                f.write(report_text)

            logger.info("ADDM report saved to: %s", output_file)
            return output_file
        except Exception as exc:
            logger.error("Failed to generate ADDM report: %s", exc)
            return None

    def generate_reports_for_window(
        self, begin_snap: int, end_snap: int
    ) -> tuple[str | None, str | None]:
        """Generate both AWR and ADDM reports for a performance test window."""
        awr_path = self.generate_awr_report(begin_snap, end_snap)
        addm_path = self.generate_addm_report(begin_snap, end_snap)

        # Send email with report info
        self._send_report_alert(begin_snap, end_snap, awr_path, addm_path)
        return awr_path, addm_path

    def _send_report_alert(
        self,
        begin_snap: int,
        end_snap: int,
        awr_path: str | None,
        addm_path: str | None,
    ) -> None:
        """Send email notification with report details."""
        html = f"""
        <h3>AWR/ADDM Report Generation Complete</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><td><strong>Begin Snapshot</strong></td><td>{begin_snap}</td></tr>
            <tr><td><strong>End Snapshot</strong></td><td>{end_snap}</td></tr>
            <tr><td><strong>AWR Report</strong></td>
                <td>{awr_path or '<span style="color:red">FAILED</span>'}</td></tr>
            <tr><td><strong>ADDM Report</strong></td>
                <td>{addm_path or '<span style="color:red">FAILED</span>'}</td></tr>
        </table>
        """
        self._alerter.send_alert(
            subject=f"AWR/ADDM Reports Generated (snaps {begin_snap}-{end_snap})",
            body_html=html,
        )

    def get_report(self) -> str:
        """Generate a text summary of recent AWR snapshots."""
        rows = self._conn.execute_query(SQL_SNAPSHOT_RANGE, {"hours": 24})
        if not rows or rows[0][0] is None:
            return "No AWR snapshots found in the last 24 hours."

        min_snap, max_snap, min_time, max_time = rows[0]
        lines = [
            "=" * 70,
            f"{'AWR SNAPSHOT SUMMARY (Last 24 Hours)':^70}",
            "=" * 70,
            f"  Earliest Snapshot: {min_snap} ({min_time})",
            f"  Latest Snapshot:   {max_snap} ({max_time})",
            f"  Snapshot Count:    {max_snap - min_snap + 1 if min_snap and max_snap else 0}",
            "=" * 70,
        ]
        return "\n".join(lines)
