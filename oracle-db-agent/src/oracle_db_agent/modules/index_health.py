"""Index health check and rebuild recommendations."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.approval import ApprovalManager
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.index_health")

SQL_INDEX_HEALTH = """
SELECT
    i.owner,
    i.index_name,
    i.table_name,
    i.index_type,
    i.status,
    i.blevel,
    i.leaf_blocks,
    i.distinct_keys,
    i.clustering_factor,
    i.num_rows,
    i.last_analyzed,
    ROUND(s.bytes / 1024 / 1024, 2) AS size_mb
FROM dba_indexes i
LEFT JOIN dba_segments s
    ON i.owner = s.owner AND i.index_name = s.segment_name AND s.segment_type LIKE 'INDEX%'
WHERE i.owner NOT IN ({schema_placeholders})
  AND i.index_type IN ('NORMAL', 'NORMAL/REV', 'FUNCTION-BASED NORMAL')
  AND (i.blevel > :blevel_threshold OR i.status != 'VALID')
ORDER BY i.blevel DESC, s.bytes DESC NULLS LAST
"""

SQL_ALL_INDEXES = """
SELECT
    i.owner,
    i.index_name,
    i.table_name,
    i.index_type,
    i.status,
    i.blevel,
    i.leaf_blocks,
    i.distinct_keys,
    i.clustering_factor,
    i.num_rows,
    i.last_analyzed,
    ROUND(NVL(s.bytes, 0) / 1024 / 1024, 2) AS size_mb
FROM dba_indexes i
LEFT JOIN dba_segments s
    ON i.owner = s.owner AND i.index_name = s.segment_name AND s.segment_type LIKE 'INDEX%'
WHERE i.owner NOT IN ({schema_placeholders})
  AND i.index_type IN ('NORMAL', 'NORMAL/REV', 'FUNCTION-BASED NORMAL')
ORDER BY s.bytes DESC NULLS LAST
FETCH FIRST 100 ROWS ONLY
"""

SQL_UNUSABLE_INDEXES = """
SELECT owner, index_name, table_name, status
FROM dba_indexes
WHERE status = 'UNUSABLE'
  AND owner NOT IN ({schema_placeholders})
ORDER BY owner, index_name
"""


@dataclass
class IndexInfo:
    owner: str
    index_name: str
    table_name: str
    index_type: str
    status: str
    blevel: int
    leaf_blocks: int
    distinct_keys: int
    clustering_factor: int
    num_rows: int
    last_analyzed: str
    size_mb: float
    recommendation: str = ""


class IndexHealthChecker:
    """Checks index health and provides rebuild recommendations."""

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
        self._blevel_threshold = config.thresholds.index_blevel_threshold
        self._system_schemas = config.system_schemas

    def _schema_placeholders(self) -> str:
        return ", ".join(f"'{s}'" for s in self._system_schemas)

    def check_index_health(self) -> list[IndexInfo]:
        """Find indexes that need attention (high blevel or invalid status)."""
        logger.info("Checking index health (blevel threshold: %d)...", self._blevel_threshold)
        sql = SQL_INDEX_HEALTH.format(schema_placeholders=self._schema_placeholders())
        rows = self._conn.execute_query(sql, {"blevel_threshold": self._blevel_threshold})

        results: list[IndexInfo] = []
        for row in rows:
            idx = IndexInfo(
                owner=row[0],
                index_name=row[1],
                table_name=row[2],
                index_type=row[3],
                status=row[4],
                blevel=int(row[5]) if row[5] else 0,
                leaf_blocks=int(row[6]) if row[6] else 0,
                distinct_keys=int(row[7]) if row[7] else 0,
                clustering_factor=int(row[8]) if row[8] else 0,
                num_rows=int(row[9]) if row[9] else 0,
                last_analyzed=str(row[10]) if row[10] else "NEVER",
                size_mb=float(row[11]) if row[11] else 0,
            )

            # Determine recommendation
            if idx.status != "VALID":
                idx.recommendation = f"REBUILD (status: {idx.status})"
            elif idx.blevel > self._blevel_threshold:
                idx.recommendation = f"REBUILD (blevel={idx.blevel}, threshold={self._blevel_threshold})"
            else:
                idx.recommendation = "MONITOR"

            results.append(idx)

        if results:
            logger.info("Found %d indexes needing attention.", len(results))
        else:
            logger.info("All indexes are healthy.")
        return results

    def find_unusable_indexes(self) -> list[IndexInfo]:
        """Find unusable indexes."""
        sql = SQL_UNUSABLE_INDEXES.format(schema_placeholders=self._schema_placeholders())
        rows = self._conn.execute_query(sql)
        results: list[IndexInfo] = []
        for row in rows:
            results.append(IndexInfo(
                owner=row[0],
                index_name=row[1],
                table_name=row[2],
                index_type="",
                status=row[3],
                blevel=0,
                leaf_blocks=0,
                distinct_keys=0,
                clustering_factor=0,
                num_rows=0,
                last_analyzed="",
                size_mb=0,
                recommendation="REBUILD (UNUSABLE)",
            ))
        return results

    def request_rebuild(self, owner: str, index_name: str) -> str:
        """Request approval to rebuild an index (destructive action)."""
        sql = f'ALTER INDEX "{owner}"."{index_name}" REBUILD ONLINE'

        if self._approval.requires_approval("rebuild_index"):
            pending = self._approval.request_approval(
                action="rebuild_index",
                description=f"Rebuild index {owner}.{index_name}",
                sql_or_command=sql,
            )
            return f"Approval requested: {pending.approval_id}"
        else:
            self._conn.execute_ddl(sql)
            return f"Index {owner}.{index_name} rebuilt."

    def audit_and_alert(self) -> list[IndexInfo]:
        """Check index health and send an alert if issues found."""
        indexes = self.check_index_health()
        unusable = self.find_unusable_indexes()
        all_issues = indexes + unusable

        if all_issues:
            self._send_index_alert(all_issues)

        return all_issues

    def _send_index_alert(self, indexes: list[IndexInfo]) -> None:
        """Send email alert for index issues."""
        rows_html = ""
        for idx in indexes[:30]:
            color = "#ff4444" if idx.status != "VALID" else "#ff8800"
            rows_html += (
                f"<tr><td>{idx.owner}</td><td>{idx.index_name}</td>"
                f"<td>{idx.table_name}</td><td>{idx.blevel}</td>"
                f"<td>{idx.size_mb:.1f}</td>"
                f"<td style='color:{color}'>{idx.recommendation}</td></tr>"
            )

        html = f"""
        <h3>Index Health Report ({len(indexes)} issues)</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Owner</th><th>Index</th><th>Table</th><th>BLevel</th>
            <th>Size MB</th><th>Recommendation</th></tr>
            {rows_html}
        </table>
        <p><em>To rebuild, run: <code>db-agent approve &lt;id&gt;</code> after requesting via the CLI.</em></p>
        """
        self._alerter.send_alert(
            subject=f"Index Health Alert: {len(indexes)} indexes need attention",
            body_html=html,
        )

    def get_report(self) -> str:
        """Generate a text report of index health."""
        indexes = self.check_index_health()
        unusable = self.find_unusable_indexes()
        all_issues = indexes + unusable

        if not all_issues:
            return "All indexes are healthy."

        lines = [
            "=" * 110,
            f"{'INDEX HEALTH REPORT':^110}",
            "=" * 110,
            f"{'Owner':<15} {'Index':<30} {'Table':<25} {'BLevel':>6} {'Size MB':>8} {'Recommendation':<25}",
            "-" * 110,
        ]
        for idx in all_issues:
            lines.append(
                f"{idx.owner:<15} {idx.index_name:<30} {idx.table_name:<25} "
                f"{idx.blevel:>6} {idx.size_mb:>7.1f} {idx.recommendation:<25}"
            )
        lines.append("-" * 110)
        lines.append(f"Total: {len(all_issues)} indexes need attention")
        lines.append("=" * 110)
        return "\n".join(lines)
