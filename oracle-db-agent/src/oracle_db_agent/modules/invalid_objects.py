"""Invalid object detection and recompilation."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.invalid_objects")

SQL_FIND_INVALID = """
SELECT
    owner,
    object_name,
    object_type,
    status,
    last_ddl_time,
    created
FROM dba_objects
WHERE status = 'INVALID'
  AND owner NOT IN ({schema_placeholders})
ORDER BY owner, object_type, object_name
"""

SQL_INVALID_COUNT_BY_TYPE = """
SELECT
    owner,
    object_type,
    COUNT(*) AS invalid_count
FROM dba_objects
WHERE status = 'INVALID'
  AND owner NOT IN ({schema_placeholders})
GROUP BY owner, object_type
ORDER BY owner, invalid_count DESC
"""

PLSQL_RECOMP_SERIAL = """
BEGIN
    UTL_RECOMP.RECOMP_SERIAL();
END;
"""

PLSQL_RECOMP_PARALLEL = """
BEGIN
    UTL_RECOMP.RECOMP_PARALLEL(:degree);
END;
"""

PLSQL_COMPILE_OBJECT = """
BEGIN
    EXECUTE IMMEDIATE 'ALTER {obj_type} "{owner}"."{name}" COMPILE';
END;
"""

PLSQL_COMPILE_PACKAGE_BODY = """
BEGIN
    EXECUTE IMMEDIATE 'ALTER PACKAGE "{owner}"."{name}" COMPILE BODY';
END;
"""


@dataclass
class InvalidObject:
    owner: str
    object_name: str
    object_type: str
    status: str
    last_ddl_time: str
    created: str


class InvalidObjectRecompiler:
    """Detects and recompiles invalid database objects."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter
        self._system_schemas = config.system_schemas

    def _schema_placeholders(self) -> str:
        return ", ".join(f"'{s}'" for s in self._system_schemas)

    def find_invalid_objects(self) -> list[InvalidObject]:
        """Find all invalid objects excluding system schemas."""
        logger.info("Scanning for invalid objects...")
        sql = SQL_FIND_INVALID.format(schema_placeholders=self._schema_placeholders())
        rows = self._conn.execute_query(sql)

        results: list[InvalidObject] = []
        for row in rows:
            obj = InvalidObject(
                owner=row[0],
                object_name=row[1],
                object_type=row[2],
                status=row[3],
                last_ddl_time=str(row[4]) if row[4] else "N/A",
                created=str(row[5]) if row[5] else "N/A",
            )
            results.append(obj)

        if results:
            logger.warning("Found %d invalid objects.", len(results))
        else:
            logger.info("No invalid objects found.")
        return results

    def recompile_all(self, parallel_degree: int = 0) -> bool:
        """Recompile all invalid objects using UTL_RECOMP.

        Args:
            parallel_degree: 0 for serial, >0 for parallel recompilation.

        Returns:
            True on success.
        """
        invalid_before = len(self.find_invalid_objects())
        if invalid_before == 0:
            logger.info("No invalid objects to recompile.")
            return True

        try:
            if parallel_degree > 0:
                logger.info("Recompiling invalid objects in parallel (degree=%d)...", parallel_degree)
                self._conn.execute_plsql(PLSQL_RECOMP_PARALLEL, {"degree": parallel_degree})
            else:
                logger.info("Recompiling invalid objects serially...")
                self._conn.execute_plsql(PLSQL_RECOMP_SERIAL)

            invalid_after = len(self.find_invalid_objects())
            logger.info(
                "Recompilation complete: %d invalid before, %d invalid after.",
                invalid_before, invalid_after,
            )

            if invalid_after > 0:
                logger.warning("%d objects still invalid after recompilation.", invalid_after)

            return True
        except Exception as exc:
            logger.error("Recompilation failed: %s", exc)
            return False

    def recompile_and_alert(self, parallel_degree: int = 0) -> bool:
        """Recompile invalid objects and send a summary email."""
        before = self.find_invalid_objects()
        if not before:
            return True

        success = self.recompile_all(parallel_degree=parallel_degree)
        after = self.find_invalid_objects()

        self._send_recompile_alert(before, after, success)
        return success

    def _send_recompile_alert(
        self,
        before: list[InvalidObject],
        after: list[InvalidObject],
        success: bool,
    ) -> None:
        """Send email alert with recompilation results."""
        still_invalid_html = ""
        for obj in after[:30]:
            still_invalid_html += (
                f"<tr><td>{obj.owner}</td><td>{obj.object_name}</td>"
                f"<td>{obj.object_type}</td></tr>"
            )

        html = f"""
        <h3>Invalid Object Recompilation Report</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><td><strong>Status</strong></td>
                <td style="color:{'green' if success else 'red'}">{
                    'Completed' if success else 'FAILED'}</td></tr>
            <tr><td><strong>Invalid Before</strong></td><td>{len(before)}</td></tr>
            <tr><td><strong>Invalid After</strong></td><td>{len(after)}</td></tr>
            <tr><td><strong>Fixed</strong></td><td>{len(before) - len(after)}</td></tr>
        </table>
        """

        if after:
            html += f"""
            <h4>Still Invalid ({len(after)} objects):</h4>
            <table border="1" cellpadding="6" cellspacing="0">
                <tr><th>Owner</th><th>Object</th><th>Type</th></tr>
                {still_invalid_html}
            </table>
            """

        self._alerter.send_alert(
            subject=f"Invalid Objects: {len(before)} found, {len(before) - len(after)} fixed",
            body_html=html,
        )

    def get_report(self) -> str:
        """Generate a text report of invalid objects."""
        objects = self.find_invalid_objects()
        if not objects:
            return "No invalid objects found."

        lines = [
            "=" * 90,
            f"{'INVALID OBJECTS REPORT':^90}",
            "=" * 90,
            f"{'Owner':<20} {'Object':<30} {'Type':<20} {'Last DDL':>18}",
            "-" * 90,
        ]
        for obj in objects:
            lines.append(
                f"{obj.owner:<20} {obj.object_name:<30} {obj.object_type:<20} {obj.last_ddl_time:>18}"
            )
        lines.append("-" * 90)
        lines.append(f"Total: {len(objects)} invalid objects")
        lines.append("=" * 90)
        return "\n".join(lines)
