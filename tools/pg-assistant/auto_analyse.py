"""Performance analysis for Oracle (AWR/V$) and PostgreSQL (pg_stat_statements).

Supports three analysis modes:
1. Live collection from V$/pg_stat_* views
2. AWR snap-ID based report generation (Oracle)
3. Uploaded report file parsing (AWR HTML/text, pg_stat_statements CSV, pgProfile)
"""

import csv
import io
import logging
import re
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
# Oracle AWR snapshot queries
# ---------------------------------------------------------------------------
_ORA_LIST_SNAPSHOTS = """
    SELECT
        snap_id,
        dbid,
        instance_number,
        TO_CHAR(begin_interval_time, 'YYYY-MM-DD HH24:MI') AS begin_time,
        TO_CHAR(end_interval_time, 'YYYY-MM-DD HH24:MI') AS end_time
    FROM dba_hist_snapshot
    ORDER BY snap_id DESC
"""

_ORA_AWR_TOP_SQL = """
    SELECT * FROM (
        SELECT
            s.sql_id,
            s.plan_hash_value,
            SUM(s.elapsed_time_delta) / 1e6 AS elapsed_sec,
            SUM(s.executions_delta) AS executions,
            SUM(s.buffer_gets_delta) AS buffer_gets,
            SUM(s.disk_reads_delta) AS disk_reads,
            DBMS_LOB.SUBSTR(t.sql_text, 200, 1) AS sql_text
        FROM dba_hist_sqlstat s
        JOIN dba_hist_sqltext t ON s.sql_id = t.sql_id AND s.dbid = t.dbid
        WHERE s.snap_id BETWEEN :begin_snap AND :end_snap
        GROUP BY s.sql_id, s.plan_hash_value,
                 DBMS_LOB.SUBSTR(t.sql_text, 200, 1)
        ORDER BY elapsed_sec DESC
    ) WHERE ROWNUM <= 20
"""

_ORA_AWR_WAIT_EVENTS = """
    SELECT * FROM (
        SELECT
            event_name AS event,
            SUM(total_waits_fg) AS total_waits,
            ROUND(SUM(time_waited_micro_fg) / 1e6, 2) AS time_waited_sec
        FROM dba_hist_system_event
        WHERE snap_id BETWEEN :begin_snap AND :end_snap
          AND wait_class != 'Idle'
        GROUP BY event_name
        ORDER BY time_waited_sec DESC
    ) WHERE ROWNUM <= 20
"""

_ORA_AWR_SYS_STATS = """
    SELECT
        stat_name AS name,
        SUM(value) AS value
    FROM dba_hist_sysstat
    WHERE snap_id BETWEEN :begin_snap AND :end_snap
      AND stat_name IN (
        'db block gets', 'consistent gets', 'physical reads',
        'redo size', 'sorts (memory)', 'sorts (disk)',
        'rows processed', 'parse count (total)', 'parse count (hard)',
        'execute count', 'user commits', 'user rollbacks'
    )
    GROUP BY stat_name
    ORDER BY stat_name
"""

# ---------------------------------------------------------------------------
# PostgreSQL pgProfile snapshot queries
# ---------------------------------------------------------------------------
_PG_LIST_PGPROFILE_SAMPLES = """
    SELECT
        sample_id,
        sample_time::text AS sample_time,
        server_name
    FROM profile.samples
    ORDER BY sample_id DESC
    LIMIT 100
"""

_PG_PGPROFILE_TOP_SQL = """
    SELECT
        queryid,
        LEFT(query, 200) AS query_text,
        calls,
        ROUND((total_exec_time / 1000)::numeric, 2) AS total_exec_sec,
        ROUND((mean_exec_time / 1000)::numeric, 4) AS mean_exec_sec,
        rows,
        shared_blks_hit,
        shared_blks_read
    FROM profile.stmt_list sl
    JOIN profile.sample_statements ss ON sl.queryid_md5 = ss.queryid_md5
    WHERE ss.sample_id BETWEEN {begin_sample} AND {end_sample}
    ORDER BY total_exec_time DESC
    LIMIT 20
"""

_PG_PGPROFILE_WAIT_EVENTS = """
    SELECT
        event_type,
        event,
        SUM(tot_waited)::numeric AS total_waited_sec,
        SUM(tot_waits) AS total_waits
    FROM profile.wait_sampling_total
    WHERE sample_id BETWEEN {begin_sample} AND {end_sample}
    GROUP BY event_type, event
    ORDER BY total_waited_sec DESC
    LIMIT 20
"""

# ---------------------------------------------------------------------------
# PostgreSQL pg_stat_statements snapshot (latest cumulative)
# ---------------------------------------------------------------------------
_PG_STAT_STATEMENTS_EXISTS = """
    SELECT COUNT(*) AS cnt
    FROM pg_extension
    WHERE extname = 'pg_stat_statements'
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

    # -- public API ----------------------------------------------------------

    def collect_data(self) -> dict[str, Any]:
        """Collect raw performance data from the database."""
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self._collect_oracle()
        return self._collect_postgresql()

    def analyse(self) -> dict[str, Any]:
        """Collect data, generate LLM analysis, and return everything."""
        raw_data = self.collect_data()
        return self._run_llm_analysis(raw_data)

    def analyse_awr_snaps(self, begin_snap: int, end_snap: int) -> dict[str, Any]:
        """Collect AWR data for a snap-ID range and generate LLM analysis."""
        raw_data = self._collect_oracle_awr(begin_snap, end_snap)
        return self._run_llm_analysis(raw_data)

    def analyse_uploaded_report(
        self, file_content: str, file_name: str
    ) -> dict[str, Any]:
        """Parse an uploaded report file and generate LLM analysis."""
        parsed = parse_uploaded_report(file_content, file_name)
        return self._run_llm_analysis_from_text(parsed)

    def list_awr_snapshots(self) -> list[dict[str, Any]]:
        """Return available AWR snapshots from DBA_HIST_SNAPSHOT."""
        result = self.db_client.execute_query(_ORA_LIST_SNAPSHOTS)
        if "error" in result:
            return []
        return result.get("rows", [])

    def list_pgprofile_samples(self) -> list[dict[str, Any]]:
        """Return available pgProfile samples from profile.samples."""
        result = self.db_client.execute_query(_PG_LIST_PGPROFILE_SAMPLES)
        if "error" in result:
            return []
        return result.get("rows", [])

    def analyse_pgprofile_snaps(
        self, begin_sample: int, end_sample: int
    ) -> dict[str, Any]:
        """Collect pgProfile data for a sample-ID range and run LLM analysis."""
        raw_data = self._collect_pgprofile(begin_sample, end_sample)
        return self._run_llm_analysis(raw_data)

    def analyse_pg_stat_latest(self) -> dict[str, Any]:
        """Collect latest pg_stat_statements data and run LLM analysis."""
        raw_data = self._collect_postgresql()
        return self._run_llm_analysis(raw_data)

    def check_pg_stat_statements(self) -> bool:
        """Check if pg_stat_statements extension is installed."""
        result = self.db_client.execute_query(_PG_STAT_STATEMENTS_EXISTS)
        if "error" in result:
            return False
        rows = result.get("rows", [])
        return bool(rows and int(rows[0].get("cnt", 0)) > 0)

    # -- internal helpers ----------------------------------------------------

    def _run_llm_analysis(self, raw_data: dict[str, Any]) -> dict[str, Any]:
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

    def _run_llm_analysis_from_text(self, report_text: str) -> dict[str, Any]:
        try:
            llm_response = self.llm_client.generate(
                prompt=report_text,
                system_prompt=ANALYSIS_SYSTEM_PROMPT,
            )
        except (ConnectionError, RuntimeError) as exc:
            llm_response = f"LLM analysis failed: {exc}"
        return {
            "raw_data": {},
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

    def _collect_oracle_awr(self, begin_snap: int, end_snap: int) -> dict[str, Any]:
        """Collect AWR historical data between two snap IDs."""
        sections: dict[str, Any] = {}
        snap_range = {":begin_snap": str(begin_snap), ":end_snap": str(end_snap)}
        queries = {
            "awr_top_sql": _ORA_AWR_TOP_SQL,
            "awr_wait_events": _ORA_AWR_WAIT_EVENTS,
            "awr_system_stats": _ORA_AWR_SYS_STATS,
        }
        for name, sql in queries.items():
            bound_sql = sql
            for placeholder, val in snap_range.items():
                bound_sql = bound_sql.replace(placeholder, val)
            result = self.db_client.execute_query(bound_sql)
            if "error" in result:
                sections[name] = {"error": result["error"]}
            else:
                sections[name] = result.get("rows", [])
        sections["db_type"] = DB_TYPE_ORACLE
        sections["snap_range"] = f"{begin_snap} - {end_snap}"
        return sections

    # -- pgProfile collection ------------------------------------------------

    def _collect_pgprofile(self, begin_sample: int, end_sample: int) -> dict[str, Any]:
        """Collect pgProfile historical data between two sample IDs."""
        sections: dict[str, Any] = {}
        queries = {
            "pgprofile_top_sql": _PG_PGPROFILE_TOP_SQL.format(
                begin_sample=begin_sample, end_sample=end_sample
            ),
            "pgprofile_wait_events": _PG_PGPROFILE_WAIT_EVENTS.format(
                begin_sample=begin_sample, end_sample=end_sample
            ),
        }
        for name, sql in queries.items():
            result = self.db_client.execute_query(sql)
            if "error" in result:
                sections[name] = {"error": result["error"]}
            else:
                sections[name] = result.get("rows", [])
        sections["db_type"] = DB_TYPE_POSTGRESQL
        sections["sample_range"] = f"{begin_sample} - {end_sample}"
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


# ---------------------------------------------------------------------------
# Report file parsing
# ---------------------------------------------------------------------------
def parse_uploaded_report(content: str, file_name: str) -> str:
    """Parse an uploaded report file and return text suitable for LLM analysis.

    Supported formats:
    - AWR HTML report (Oracle)
    - AWR text report (Oracle)
    - pg_stat_statements CSV export
    - pgProfile text/HTML report
    - Plain text report
    """
    lower_name = file_name.lower()

    if lower_name.endswith(".csv"):
        return _parse_csv_report(content, file_name)
    if lower_name.endswith((".html", ".htm")):
        return _parse_html_report(content, file_name)
    return _parse_text_report(content, file_name)


def _parse_csv_report(content: str, file_name: str) -> str:
    """Parse a CSV file (e.g. pg_stat_statements export)."""
    parts = [f"UPLOADED REPORT: {file_name}\n{'=' * 60}\n"]
    parts.append("Format: CSV (likely pg_stat_statements or similar export)\n")

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    if not rows:
        parts.append("(empty CSV)")
        return "\n".join(parts)

    parts.append(f"Columns: {', '.join(rows[0].keys())}")
    parts.append(f"Total rows: {len(rows)}\n")

    for i, row in enumerate(rows[:30]):
        parts.append(f"  [{i + 1}] {_format_row(row)}")
    if len(rows) > 30:
        parts.append(f"  ... and {len(rows) - 30} more rows")

    return "\n".join(parts)


def _parse_html_report(content: str, file_name: str) -> str:
    """Parse an HTML report (AWR or pgProfile) by extracting text content."""
    parts = [f"UPLOADED REPORT: {file_name}\n{'=' * 60}\n"]

    if (
        "AWR" in content[:2000].upper()
        or "WORKLOAD REPOSITORY" in content[:2000].upper()
    ):
        parts.append("Format: Oracle AWR HTML Report\n")
    elif (
        "pgprofile" in content[:2000].lower() or "pg_profile" in content[:2000].lower()
    ):
        parts.append("Format: pgProfile HTML Report\n")
    else:
        parts.append("Format: HTML Report\n")

    # Strip HTML tags to get text content
    text = re.sub(
        r"<style[^>]*>.*?</style>", "", content, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(
        r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"\s+", " ", text).strip()

    # Truncate to a reasonable size for LLM context
    max_chars = 15000
    if len(text) > max_chars:
        parts.append(text[:max_chars])
        parts.append(f"\n... (truncated, {len(text)} total characters)")
    else:
        parts.append(text)

    return "\n".join(parts)


def _parse_text_report(content: str, file_name: str) -> str:
    """Parse a plain text report (AWR text, pgProfile text, etc.)."""
    parts = [f"UPLOADED REPORT: {file_name}\n{'=' * 60}\n"]

    if (
        "AWR" in content[:2000].upper()
        or "WORKLOAD REPOSITORY" in content[:2000].upper()
    ):
        parts.append("Format: Oracle AWR Text Report\n")
    elif (
        "pgprofile" in content[:2000].lower() or "pg_profile" in content[:2000].lower()
    ):
        parts.append("Format: pgProfile Text Report\n")
    elif "pg_stat_statements" in content[:2000].lower():
        parts.append("Format: pg_stat_statements Report\n")
    else:
        parts.append("Format: Text Report\n")

    max_chars = 15000
    if len(content) > max_chars:
        parts.append(content[:max_chars])
        parts.append(f"\n... (truncated, {len(content)} total characters)")
    else:
        parts.append(content)

    return "\n".join(parts)
