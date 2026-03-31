"""Automated optimizer statistics collection."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.stats_collector")

SQL_STALE_STATS = """
SELECT
    owner,
    table_name,
    num_rows,
    last_analyzed,
    stale_stats
FROM dba_tab_statistics
WHERE (stale_stats = 'YES' OR last_analyzed IS NULL)
  AND owner IN ({schema_placeholders})
  AND table_name NOT IN ({exclude_placeholders})
  AND object_type = 'TABLE'
ORDER BY num_rows DESC NULLS FIRST
"""

SQL_STALE_STATS_NO_EXCLUDE = """
SELECT
    owner,
    table_name,
    num_rows,
    last_analyzed,
    stale_stats
FROM dba_tab_statistics
WHERE (stale_stats = 'YES' OR last_analyzed IS NULL)
  AND owner IN ({schema_placeholders})
  AND object_type = 'TABLE'
ORDER BY num_rows DESC NULLS FIRST
"""

PLSQL_GATHER_SCHEMA_STATS = """
BEGIN
    DBMS_STATS.GATHER_SCHEMA_STATS(
        ownname          => :schema_name,
        estimate_percent => {estimate_pct},
        degree           => :degree,
        cascade          => TRUE,
        no_invalidate    => FALSE
    );
END;
"""

PLSQL_GATHER_TABLE_STATS = """
BEGIN
    DBMS_STATS.GATHER_TABLE_STATS(
        ownname          => :schema_name,
        tabname          => :table_name,
        estimate_percent => {estimate_pct},
        degree           => :degree,
        cascade          => TRUE,
        no_invalidate    => FALSE
    );
END;
"""


@dataclass
class StaleTableInfo:
    """Table with stale or missing statistics."""

    owner: str
    table_name: str
    num_rows: int | None
    last_analyzed: datetime | None
    stale_stats: str


class StatsCollector:
    """Gathers optimizer statistics on tables with stale or missing stats."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter
        self._stats_cfg = config.stats_collection

    def _schema_placeholders(self) -> str:
        return ", ".join(f"'{s}'" for s in self._stats_cfg.schemas)

    def _exclude_placeholders(self) -> str:
        if not self._stats_cfg.exclude_tables:
            return "'__NONE__'"
        return ", ".join(f"'{t}'" for t in self._stats_cfg.exclude_tables)

    def is_in_maintenance_window(self) -> bool:
        """Check if the current time is within the configured maintenance window."""
        now = datetime.now()
        day_abbr = now.strftime("%a").upper()[:3]
        if day_abbr not in self._stats_cfg.maintenance_window.days:
            return False

        start_parts = self._stats_cfg.maintenance_window.start.split(":")
        end_parts = self._stats_cfg.maintenance_window.end.split(":")
        start_minutes = int(start_parts[0]) * 60 + int(start_parts[1])
        end_minutes = int(end_parts[0]) * 60 + int(end_parts[1])
        now_minutes = now.hour * 60 + now.minute

        # Handle overnight windows (e.g., 22:00 - 05:00)
        if start_minutes > end_minutes:
            return now_minutes >= start_minutes or now_minutes < end_minutes
        return start_minutes <= now_minutes < end_minutes

    def find_stale_stats(self) -> list[StaleTableInfo]:
        """Find all tables with stale or missing statistics."""
        logger.info("Scanning for tables with stale/missing statistics...")

        if self._stats_cfg.exclude_tables:
            sql = SQL_STALE_STATS.format(
                schema_placeholders=self._schema_placeholders(),
                exclude_placeholders=self._exclude_placeholders(),
            )
        else:
            sql = SQL_STALE_STATS_NO_EXCLUDE.format(
                schema_placeholders=self._schema_placeholders(),
            )

        rows = self._conn.execute_query(sql)
        results: list[StaleTableInfo] = []
        for row in rows:
            info = StaleTableInfo(
                owner=row[0],
                table_name=row[1],
                num_rows=int(row[2]) if row[2] is not None else None,
                last_analyzed=row[3],
                stale_stats=row[4] or "N/A",
            )
            results.append(info)

        if results:
            logger.info("Found %d tables with stale/missing statistics.", len(results))
        else:
            logger.info("All table statistics are up to date.")
        return results

    def gather_schema_stats(self, schema_name: str) -> bool:
        """Gather statistics for an entire schema."""
        estimate_pct = (
            "DBMS_STATS.AUTO_SAMPLE_SIZE"
            if self._stats_cfg.estimate_percent == 0
            else str(self._stats_cfg.estimate_percent)
        )
        plsql = PLSQL_GATHER_SCHEMA_STATS.format(estimate_pct=estimate_pct)
        try:
            logger.info("Gathering statistics for schema %s (degree=%d)...",
                        schema_name, self._stats_cfg.parallel_degree)
            self._conn.execute_plsql(
                plsql,
                {"schema_name": schema_name, "degree": self._stats_cfg.parallel_degree},
            )
            logger.info("Statistics gathered for schema %s.", schema_name)
            return True
        except Exception as exc:
            logger.error("Failed to gather stats for schema %s: %s", schema_name, exc)
            return False

    def gather_table_stats(self, schema_name: str, table_name: str) -> bool:
        """Gather statistics for a specific table."""
        estimate_pct = (
            "DBMS_STATS.AUTO_SAMPLE_SIZE"
            if self._stats_cfg.estimate_percent == 0
            else str(self._stats_cfg.estimate_percent)
        )
        plsql = PLSQL_GATHER_TABLE_STATS.format(estimate_pct=estimate_pct)
        try:
            logger.info("Gathering statistics for %s.%s...", schema_name, table_name)
            self._conn.execute_plsql(
                plsql,
                {
                    "schema_name": schema_name,
                    "table_name": table_name,
                    "degree": self._stats_cfg.parallel_degree,
                },
            )
            logger.info("Statistics gathered for %s.%s.", schema_name, table_name)
            return True
        except Exception as exc:
            logger.error("Failed to gather stats for %s.%s: %s", schema_name, table_name, exc)
            return False

    def gather_stale_stats(self, force: bool = False) -> int:
        """Gather statistics for all tables with stale/missing stats.

        Args:
            force: If True, ignore maintenance window check.

        Returns:
            Number of schemas processed.
        """
        if not force and not self.is_in_maintenance_window():
            logger.info("Outside maintenance window; skipping stats collection. Use --force to override.")
            return 0

        count = 0
        for schema in self._stats_cfg.schemas:
            if self.gather_schema_stats(schema):
                count += 1
        return count

    def gather_and_alert(self, force: bool = False) -> int:
        """Gather stale stats and send a summary email."""
        stale_before = self.find_stale_stats()
        count = self.gather_stale_stats(force=force)

        if count > 0 or stale_before:
            self._send_stats_alert(stale_before, count)
        return count

    def _send_stats_alert(self, stale_tables: list[StaleTableInfo], schemas_processed: int) -> None:
        """Send email summary of stats collection."""
        rows_html = ""
        for t in stale_tables[:50]:  # Limit to 50 for email
            last = t.last_analyzed.strftime("%Y-%m-%d %H:%M") if t.last_analyzed else "NEVER"
            rows_html += (
                f"<tr><td>{t.owner}</td><td>{t.table_name}</td>"
                f"<td>{t.num_rows or 'N/A'}</td><td>{last}</td>"
                f"<td>{t.stale_stats}</td></tr>"
            )

        html = f"""
        <h3>Statistics Collection Summary</h3>
        <p>Schemas processed: <strong>{schemas_processed}</strong></p>
        <p>Tables with stale/missing stats (before collection): <strong>{len(stale_tables)}</strong></p>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Owner</th><th>Table</th><th>Rows</th><th>Last Analyzed</th><th>Stale?</th></tr>
            {rows_html}
        </table>
        """
        self._alerter.send_alert(
            subject=f"Statistics Collection Complete ({schemas_processed} schemas)",
            body_html=html,
        )

    def get_report(self) -> str:
        """Generate a text report of stale statistics."""
        stale = self.find_stale_stats()
        if not stale:
            return "All table statistics are up to date."

        lines = [
            "=" * 100,
            f"{'STALE/MISSING STATISTICS REPORT':^100}",
            "=" * 100,
            f"{'Owner':<20} {'Table':<30} {'Rows':>12} {'Last Analyzed':<20} {'Stale':>6}",
            "-" * 100,
        ]
        for t in stale:
            last = t.last_analyzed.strftime("%Y-%m-%d %H:%M") if t.last_analyzed else "NEVER"
            rows_str = str(t.num_rows) if t.num_rows is not None else "N/A"
            lines.append(
                f"{t.owner:<20} {t.table_name:<30} {rows_str:>12} {last:<20} {t.stale_stats:>6}"
            )
        lines.append("=" * 100)
        return "\n".join(lines)
