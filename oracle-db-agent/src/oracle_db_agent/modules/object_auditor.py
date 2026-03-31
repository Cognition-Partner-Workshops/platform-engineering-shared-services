"""Detect non-system objects in SYSTEM/SYSAUX tablespaces."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.object_auditor")

SQL_NON_SYSTEM_OBJECTS = """
SELECT
    owner,
    segment_name,
    segment_type,
    tablespace_name,
    ROUND(bytes / 1024 / 1024, 2) AS size_mb
FROM dba_segments
WHERE tablespace_name IN ('SYSTEM', 'SYSAUX')
  AND owner NOT IN ({schema_placeholders})
ORDER BY bytes DESC
"""

SQL_GENERATE_MOVE_DDL_TABLE = """
SELECT
    'ALTER TABLE "' || owner || '"."' || segment_name || '" MOVE TABLESPACE USERS;' AS move_ddl,
    owner,
    segment_name,
    ROUND(bytes / 1024 / 1024, 2) AS size_mb
FROM dba_segments
WHERE tablespace_name IN ('SYSTEM', 'SYSAUX')
  AND segment_type IN ('TABLE', 'TABLE PARTITION', 'TABLE SUBPARTITION')
  AND owner NOT IN ({schema_placeholders})
ORDER BY bytes DESC
"""

SQL_GENERATE_MOVE_DDL_INDEX = """
SELECT
    'ALTER INDEX "' || owner || '"."' || segment_name || '" REBUILD TABLESPACE USERS;' AS move_ddl,
    owner,
    segment_name,
    ROUND(bytes / 1024 / 1024, 2) AS size_mb
FROM dba_segments
WHERE tablespace_name IN ('SYSTEM', 'SYSAUX')
  AND segment_type IN ('INDEX', 'INDEX PARTITION', 'INDEX SUBPARTITION')
  AND owner NOT IN ({schema_placeholders})
ORDER BY bytes DESC
"""

SQL_GENERATE_MOVE_DDL_LOB = """
SELECT
    'ALTER TABLE "' || l.owner || '"."' || l.table_name ||
    '" MOVE LOB ("' || l.column_name || '") STORE AS (TABLESPACE USERS);' AS move_ddl,
    s.owner,
    s.segment_name,
    ROUND(s.bytes / 1024 / 1024, 2) AS size_mb
FROM dba_segments s
JOIN dba_lobs l ON s.owner = l.owner AND s.segment_name = l.segment_name
WHERE s.tablespace_name IN ('SYSTEM', 'SYSAUX')
  AND s.segment_type = 'LOBSEGMENT'
  AND s.owner NOT IN ({schema_placeholders})
ORDER BY s.bytes DESC
"""


@dataclass
class NonSystemObject:
    """A non-system object found in SYSTEM/SYSAUX tablespace."""

    owner: str
    segment_name: str
    segment_type: str
    tablespace_name: str
    size_mb: float


class ObjectAuditor:
    """Detects non-system objects in SYSTEM/SYSAUX tablespaces."""

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
        """Generate quoted comma-separated list of system schemas for SQL IN clause."""
        return ", ".join(f"'{s}'" for s in self._system_schemas)

    def find_non_system_objects(self) -> list[NonSystemObject]:
        """Find all non-system objects in SYSTEM/SYSAUX tablespaces."""
        logger.info("Scanning for non-system objects in SYSTEM/SYSAUX tablespaces...")
        sql = SQL_NON_SYSTEM_OBJECTS.format(schema_placeholders=self._schema_placeholders())
        rows = self._conn.execute_query(sql)
        results: list[NonSystemObject] = []
        for row in rows:
            obj = NonSystemObject(
                owner=row[0],
                segment_name=row[1],
                segment_type=row[2],
                tablespace_name=row[3],
                size_mb=float(row[4]),
            )
            results.append(obj)

        if results:
            total_mb = sum(o.size_mb for o in results)
            logger.warning(
                "Found %d non-system objects in SYSTEM/SYSAUX (total %.1f MB)",
                len(results), total_mb,
            )
        else:
            logger.info("No non-system objects found in SYSTEM/SYSAUX tablespaces.")
        return results

    def generate_move_ddl(self, target_tablespace: str = "USERS") -> list[str]:
        """Generate DDL statements to move non-system objects out of SYSTEM/SYSAUX.

        Returns a list of SQL DDL statements.
        """
        ddl_statements: list[str] = []
        placeholders = self._schema_placeholders()

        # Tables
        sql = SQL_GENERATE_MOVE_DDL_TABLE.format(schema_placeholders=placeholders)
        for row in self._conn.execute_query(sql):
            ddl = str(row[0]).replace("TABLESPACE USERS", f"TABLESPACE {target_tablespace}")
            ddl_statements.append(ddl)

        # Indexes
        sql = SQL_GENERATE_MOVE_DDL_INDEX.format(schema_placeholders=placeholders)
        for row in self._conn.execute_query(sql):
            ddl = str(row[0]).replace("TABLESPACE USERS", f"TABLESPACE {target_tablespace}")
            ddl_statements.append(ddl)

        # LOBs
        sql = SQL_GENERATE_MOVE_DDL_LOB.format(schema_placeholders=placeholders)
        for row in self._conn.execute_query(sql):
            ddl = str(row[0]).replace("TABLESPACE USERS", f"TABLESPACE {target_tablespace}")
            ddl_statements.append(ddl)

        logger.info("Generated %d move DDL statements.", len(ddl_statements))
        return ddl_statements

    def audit_and_alert(self) -> list[NonSystemObject]:
        """Run the audit and send an email alert if objects are found."""
        objects = self.find_non_system_objects()
        if not objects:
            return objects

        ddl_statements = self.generate_move_ddl()
        self._send_audit_alert(objects, ddl_statements)
        return objects

    def _send_audit_alert(
        self, objects: list[NonSystemObject], ddl_statements: list[str]
    ) -> None:
        """Send email alert for non-system objects found."""
        rows_html = ""
        for obj in objects:
            rows_html += (
                f"<tr><td>{obj.owner}</td><td>{obj.segment_name}</td>"
                f"<td>{obj.segment_type}</td><td>{obj.tablespace_name}</td>"
                f"<td>{obj.size_mb:.2f}</td></tr>"
            )

        ddl_html = ""
        if ddl_statements:
            ddl_html = "<h4>Suggested Move DDL:</h4><pre>" + "\n".join(ddl_statements) + "</pre>"

        html = f"""
        <h3>Non-System Objects in SYSTEM/SYSAUX Tablespace</h3>
        <p>Found {len(objects)} non-system objects (total {sum(o.size_mb for o in objects):.1f} MB):</p>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Owner</th><th>Object Name</th><th>Type</th><th>Tablespace</th><th>Size (MB)</th></tr>
            {rows_html}
        </table>
        {ddl_html}
        <p><em>Review and approve move operations via: <code>db-agent approve &lt;id&gt;</code></em></p>
        """
        self._alerter.send_alert(
            subject=f"Non-System Objects Found in SYSTEM/SYSAUX ({len(objects)} objects)",
            body_html=html,
        )

    def get_report(self) -> str:
        """Generate a text report."""
        objects = self.find_non_system_objects()
        if not objects:
            return "No non-system objects found in SYSTEM/SYSAUX tablespaces."

        lines = [
            "=" * 100,
            f"{'NON-SYSTEM OBJECTS IN SYSTEM/SYSAUX':^100}",
            "=" * 100,
            f"{'Owner':<20} {'Object':<30} {'Type':<20} {'Tablespace':<12} {'Size MB':>10}",
            "-" * 100,
        ]
        for obj in objects:
            lines.append(
                f"{obj.owner:<20} {obj.segment_name:<30} {obj.segment_type:<20} "
                f"{obj.tablespace_name:<12} {obj.size_mb:>9.2f}"
            )
        lines.append("-" * 100)
        lines.append(f"Total: {len(objects)} objects, {sum(o.size_mb for o in objects):.1f} MB")
        lines.append("=" * 100)
        return "\n".join(lines)
