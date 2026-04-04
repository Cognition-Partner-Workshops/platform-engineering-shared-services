"""Performance analysis for Oracle (AWR/V$) and PostgreSQL (pg_stat_statements)."""

import logging
from typing import Any

from db_client import BaseDBClient, DB_TYPE_ORACLE, DB_TYPE_POSTGRESQL
from llm_client import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Oracle V$ performance queries
# ---------------------------------------------------------------------------
_ORA_TOP_SQL = """
    SELECT * FROM (
        SELECT
            sql_id,
            plan_hash_value,
            ROUND(elapsed_time / 1e6, 2) AS elapsed_sec,
            executions,
            buffer_gets,
            disk_reads,
            SUBSTR(sql_text, 1, 200) AS sql_text
        FROM v$sql
        ORDER BY elapsed_time DESC
    ) WHERE ROWNUM <= 20
"""

_ORA_WAIT_EVENTS = """
    SELECT * FROM (
        SELECT
            event,
            total_waits,
            ROUND(time_waited / 100, 2) AS time_waited_sec,
            ROUND(average_wait / 100, 4) AS avg_wait_sec
        FROM v$system_event
        WHERE wait_class != 'Idle'
        ORDER BY time_waited DESC
    ) WHERE ROWNUM <= 20
"""

_ORA_SYS_STATS = """
    SELECT name, value
    FROM v$sysstat
    WHERE name IN (
        'db block gets', 'consistent gets', 'physical reads',
        'redo size', 'sorts (memory)', 'sorts (disk)',
        'rows processed', 'parse count (total)', 'parse count (hard)',
        'execute count', 'user commits', 'user rollbacks'
    )
    ORDER BY name
"""

_ORA_SGA = """
    SELECT name, ROUND(bytes / 1048576, 2) AS size_mb
    FROM v$sgainfo
    WHERE name IN (
        'Fixed SGA Size', 'Redo Buffers', 'Buffer Cache Size',
        'Shared Pool Size', 'Large Pool Size', 'Java Pool Size',
        'Streams Pool Size', 'Maximum SGA Size'
    )
    ORDER BY name
"""

_ORA_TABLESPACE_IO = """
    SELECT * FROM (
        SELECT
            ts.name AS tablespace_name,
            SUM(fs.phyrds) AS physical_reads,
            SUM(fs.phywrts) AS physical_writes,
            ROUND(SUM(fs.readtim) / 100, 2) AS read_time_sec,
            ROUND(SUM(fs.writetim) / 100, 2) AS write_time_sec
        FROM v$filestat fs
        JOIN v$datafile df ON fs.file# = df.file#
        JOIN v$tablespace ts ON df.ts# = ts.ts#
        GROUP BY ts.name
        ORDER BY physical_reads + physical_writes DESC
    ) WHERE ROWNUM <= 20
"""

# ---------------------------------------------------------------------------
# PostgreSQL performance queries
# ---------------------------------------------------------------------------
_PG_TOP_QUERIES = """
    SELECT
        queryid,
        LEFT(query, 200) AS query_text,
        calls,
        ROUND((total_exec_time / 1000)::numeric, 2) AS total_exec_sec,
        ROUND((mean_exec_time / 1000)::numeric, 4) AS mean_exec_sec,
        rows,
        shared_blks_hit,
        shared_blks_read,
        CASE WHEN shared_blks_hit + shared_blks_read > 0
            THEN ROUND(
                shared_blks_hit::numeric
                / (shared_blks_hit + shared_blks_read) * 100, 2
            )
            ELSE 100
        END AS cache_hit_pct
    FROM pg_stat_statements
    ORDER BY total_exec_time DESC
    LIMIT 20
"""

_PG_TABLE_STATS = """
    SELECT
        schemaname, relname,
        seq_scan, seq_tup_read,
        idx_scan, idx_tup_fetch,
        n_tup_ins, n_tup_upd, n_tup_del,
        n_live_tup, n_dead_tup,
        last_vacuum, last_autovacuum,
        last_analyze, last_autoanalyze
    FROM pg_stat_user_tables
    ORDER BY seq_scan + COALESCE(idx_scan, 0) DESC
    LIMIT 20
"""

_PG_DB_STATS = """
    SELECT
        datname,
        numbackends,
        xact_commit, xact_rollback,
        blks_read, blks_hit,
        CASE WHEN blks_hit + blks_read > 0
            THEN ROUND(blks_hit::numeric / (blks_hit + blks_read) * 100, 2)
            ELSE 100
        END AS cache_hit_pct,
        tup_returned, tup_fetched,
        tup_inserted, tup_updated, tup_deleted,
        temp_files, temp_bytes
    FROM pg_stat_database
    WHERE datname = current_database()
"""

_PG_BGWRITER = """
    SELECT
        checkpoints_timed, checkpoints_req,
        buffers_checkpoint, buffers_clean, buffers_backend,
        maxwritten_clean
    FROM pg_stat_bgwriter
"""

_PG_UNUSED_INDEXES = """
    SELECT
        schemaname, relname, indexrelname,
        idx_scan, idx_tup_read, idx_tup_fetch,
        pg_relation_size(indexrelid) / 1048576 AS index_size_mb
    FROM pg_stat_user_indexes
    WHERE idx_scan = 0
    ORDER BY pg_relation_size(indexrelid) DESC
    LIMIT 20
"""

ANALYSIS_SYSTEM_PROMPT = (
    "You are a senior database performance engineer. "
    "Analyze the following database performance data and provide:\n"
    "1. **Executive Summary** (2-3 sentences)\n"
    "2. **Key Findings** (bullet list of important observations)\n"
    "3. **Top Issues** (ranked by severity)\n"
    "4. **Action Plan** (prioritized recommendations with specific SQL or steps)\n\n"
    "Be concise and actionable. Use markdown formatting."
)


# ---------------------------------------------------------------------------
# Analyser
# ---------------------------------------------------------------------------
class PerformanceAnalyser:
    """Collects DB performance data and generates LLM-powered analysis."""

    def __init__(
        self,
        db_client: BaseDBClient,
        llm_client: LLMClient,
    ) -> None:
        self.db_client = db_client
        self.llm_client = llm_client

    def collect_data(self) -> dict[str, Any]:
        """Collect raw performance data from the database."""
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self._collect_oracle()
        return self._collect_postgresql()

    def analyse(self) -> dict[str, Any]:
        """Collect data, generate LLM analysis, and return everything."""
        raw_data = self.collect_data()
        report_text = self._format_report(raw_data)

        try:
            llm_response = self.llm_client.generate(
                prompt=report_text,
                system_prompt=ANALYSIS_SYSTEM_PROMPT,
            )
        except (ConnectionError, RuntimeError) as exc:
            llm_response = f"LLM analysis failed: {exc}"

        return {
            "raw_data": raw_data,
            "report_text": report_text,
            "analysis": llm_response,
        }

    # -- Oracle collection ---------------------------------------------------

    def _collect_oracle(self) -> dict[str, Any]:
        sections: dict[str, Any] = {}
        queries = {
            "top_sql": _ORA_TOP_SQL,
            "wait_events": _ORA_WAIT_EVENTS,
            "system_stats": _ORA_SYS_STATS,
            "sga_info": _ORA_SGA,
            "tablespace_io": _ORA_TABLESPACE_IO,
        }
        for name, sql in queries.items():
            result = self.db_client.execute_query(sql)
            if "error" in result:
                sections[name] = {"error": result["error"]}
            else:
                sections[name] = result.get("rows", [])
        sections["db_type"] = DB_TYPE_ORACLE
        return sections

    # -- PostgreSQL collection -----------------------------------------------

    def _collect_postgresql(self) -> dict[str, Any]:
        sections: dict[str, Any] = {}
        queries = {
            "top_queries": _PG_TOP_QUERIES,
            "table_stats": _PG_TABLE_STATS,
            "database_stats": _PG_DB_STATS,
            "bgwriter_stats": _PG_BGWRITER,
            "unused_indexes": _PG_UNUSED_INDEXES,
        }
        for name, sql in queries.items():
            result = self.db_client.execute_query(sql)
            if "error" in result:
                sections[name] = {"error": result["error"]}
            else:
                sections[name] = result.get("rows", [])
        sections["db_type"] = DB_TYPE_POSTGRESQL
        return sections

    # -- Report formatting ---------------------------------------------------

    def _format_report(self, data: dict[str, Any]) -> str:
        """Format collected data into a human-readable report for the LLM."""
        db_type = data.get("db_type", "unknown")
        parts = [f"DATABASE PERFORMANCE REPORT ({db_type.upper()})\n{'=' * 60}\n"]

        for section_name, section_data in data.items():
            if section_name == "db_type":
                continue
            parts.append(f"\n--- {section_name.upper().replace('_', ' ')} ---")
            if isinstance(section_data, dict) and "error" in section_data:
                parts.append(f"  ERROR: {section_data['error']}")
            elif isinstance(section_data, list):
                if not section_data:
                    parts.append("  (no data)")
                else:
                    for i, row in enumerate(section_data[:15]):
                        parts.append(f"  [{i + 1}] {_format_row(row)}")
                    if len(section_data) > 15:
                        parts.append(f"  ... and {len(section_data) - 15} more rows")
            else:
                parts.append(f"  {section_data}")

        return "\n".join(parts)


def _format_row(row: dict[str, Any]) -> str:
    """Format a single row dict into a compact string."""
    items = []
    for k, v in row.items():
        if v is None:
            continue
        items.append(f"{k}={v}")
    return ", ".join(items)
