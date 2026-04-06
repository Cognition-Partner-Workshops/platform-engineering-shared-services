"""SQL Tuning Advisor for Oracle and PostgreSQL.

Accepts a SQL statement, runs EXPLAIN PLAN, collects relevant metadata
(table DDL, existing indexes, stats), and uses the LLM to generate
specific tuning recommendations.
"""

import logging
from typing import Any

from db_client import BaseDBClient, DB_TYPE_ORACLE
from llm_client import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Oracle EXPLAIN helpers
# ---------------------------------------------------------------------------
_ORA_EXPLAIN_PLAN = "EXPLAIN PLAN FOR {sql}"

_ORA_DISPLAY_PLAN = """
    SELECT plan_table_output
    FROM TABLE(DBMS_XPLAN.DISPLAY('PLAN_TABLE', NULL, 'ALL'))
"""

_ORA_TABLE_DDL = """
    SELECT
        column_name,
        data_type,
        data_length,
        data_precision,
        nullable,
        num_distinct,
        num_nulls,
        density,
        histogram
    FROM all_tab_col_statistics
    WHERE owner = '{owner}'
        AND table_name = '{table_name}'
    ORDER BY column_id
"""

_ORA_TABLE_INDEXES = """
    SELECT
        i.index_name,
        i.index_type,
        i.uniqueness,
        i.status,
        i.num_rows,
        i.distinct_keys,
        i.clustering_factor,
        TO_CHAR(i.last_analyzed, 'YYYY-MM-DD HH24:MI') AS last_analyzed,
        LISTAGG(c.column_name, ', ')
            WITHIN GROUP (ORDER BY c.column_position) AS columns
    FROM all_indexes i
    JOIN all_ind_columns c
        ON i.index_name = c.index_name AND i.owner = c.index_owner
    WHERE i.table_owner = '{owner}'
        AND i.table_name = '{table_name}'
    GROUP BY i.index_name, i.index_type, i.uniqueness, i.status,
             i.num_rows, i.distinct_keys, i.clustering_factor, i.last_analyzed
    ORDER BY i.index_name
"""

_ORA_TABLE_STATS = """
    SELECT
        table_name,
        num_rows,
        blocks,
        avg_row_len,
        TO_CHAR(last_analyzed, 'YYYY-MM-DD HH24:MI') AS last_analyzed,
        stale_stats,
        sample_size
    FROM all_tab_statistics
    WHERE owner = '{owner}'
        AND table_name = '{table_name}'
"""

_ORA_EXTRACT_TABLES = """
    SELECT DISTINCT
        p.object_owner AS owner,
        p.object_name AS table_name
    FROM plan_table p
    WHERE p.object_type = 'TABLE'
        AND p.object_owner IS NOT NULL
"""

# ---------------------------------------------------------------------------
# PostgreSQL EXPLAIN helpers
# ---------------------------------------------------------------------------
_PG_EXPLAIN = "EXPLAIN (ANALYZE false, COSTS true, FORMAT TEXT) {sql}"
_PG_EXPLAIN_ANALYZE = (
    "EXPLAIN (ANALYZE true, COSTS true, BUFFERS true, FORMAT TEXT) {sql}"
)

_PG_TABLE_COLUMNS = """
    SELECT
        column_name,
        data_type,
        is_nullable,
        column_default,
        character_maximum_length
    FROM information_schema.columns
    WHERE table_schema = '{schema}'
        AND table_name = '{table_name}'
    ORDER BY ordinal_position
"""

_PG_TABLE_INDEXES = """
    SELECT
        indexname,
        indexdef
    FROM pg_indexes
    WHERE schemaname = '{schema}'
        AND tablename = '{table_name}'
    ORDER BY indexname
"""

_PG_TABLE_STATS = """
    SELECT
        relname,
        n_live_tup,
        n_dead_tup,
        seq_scan,
        seq_tup_read,
        idx_scan,
        idx_tup_fetch,
        last_vacuum::text,
        last_autovacuum::text,
        last_analyze::text,
        last_autoanalyze::text
    FROM pg_stat_user_tables
    WHERE schemaname = '{schema}'
        AND relname = '{table_name}'
"""

_PG_COLUMN_STATS = """
    SELECT
        attname AS column_name,
        n_distinct,
        null_frac,
        avg_width,
        correlation
    FROM pg_stats
    WHERE schemaname = '{schema}'
        AND tablename = '{table_name}'
    ORDER BY attname
"""

# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------
TUNING_SYSTEM_PROMPT = (
    "You are a senior DBA and SQL tuning expert. You have been given a SQL "
    "statement, its execution plan, table structure, existing indexes, and "
    "column/table statistics.\n\n"
    "Produce the following sections:\n\n"
    "## Execution Plan Analysis\n"
    "Walk through the plan step by step. Identify:\n"
    "- Full table scans (and whether they are justified)\n"
    "- Nested loop joins vs hash joins (and whether the choice is optimal)\n"
    "- Sort operations that could be avoided\n"
    "- High-cost steps\n"
    "- Estimated vs actual row discrepancies (if ANALYZE data available)\n\n"
    "## Root Cause\n"
    "Explain WHY the query may be slow. Reference specific plan steps, "
    "missing indexes, stale statistics, or suboptimal SQL patterns.\n\n"
    "## Recommended Indexes\n"
    "For each suggested index:\n"
    "- Provide the exact `CREATE INDEX` statement\n"
    "- Explain which plan step it improves\n"
    "- Note if a composite index is better than multiple single-column indexes\n\n"
    "## SQL Rewrite Suggestions\n"
    "If the SQL can be rewritten for better performance:\n"
    "- Show the rewritten SQL in a code block\n"
    "- Explain what changed and why it is faster\n"
    "- Consider: subquery elimination, EXISTS vs IN, join reordering, "
    "predicate pushdown, avoiding SELECT *\n\n"
    "## Statistics & Maintenance\n"
    "If statistics are stale or missing, provide exact commands:\n"
    "- Oracle: `EXEC DBMS_STATS.GATHER_TABLE_STATS(...)` with proper params\n"
    "- PostgreSQL: `ANALYZE table_name;` or `VACUUM ANALYZE table_name;`\n\n"
    "## Summary Action Plan\n"
    "Numbered list of actions in priority order. Each with:\n"
    "- The exact SQL command to run\n"
    "- Expected improvement\n\n"
    "IMPORTANT: Be SPECIFIC. Reference table names, column names, and index "
    "names. Provide copy-paste-ready SQL. Use markdown with code blocks."
)


# ---------------------------------------------------------------------------
# SQLTuningAdvisor class
# ---------------------------------------------------------------------------
class SQLTuningAdvisor:
    """Analyses a SQL statement and provides tuning recommendations."""

    def __init__(
        self,
        db_client: BaseDBClient,
        llm_client: LLMClient,
    ) -> None:
        self.db_client = db_client
        self.llm_client = llm_client

    def analyse_sql(self, sql: str, run_analyze: bool = False) -> dict[str, Any]:
        """Run EXPLAIN on the SQL, collect metadata, and get LLM recommendations.

        Args:
            sql: The SQL statement to analyse.
            run_analyze: If True, use EXPLAIN ANALYZE (PostgreSQL) which
                actually executes the query. Use with caution on write queries.
        """
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self._analyse_oracle(sql)
        return self._analyse_postgresql(sql, run_analyze)

    # -- Oracle ---------------------------------------------------------------

    def _analyse_oracle(self, sql: str) -> dict[str, Any]:
        sections: dict[str, str] = {}

        # 1. Run EXPLAIN PLAN
        explain_result = self.db_client.execute_statement(
            _ORA_EXPLAIN_PLAN.format(sql=sql)
        )
        if not explain_result.get("success"):
            return {
                "error": f"EXPLAIN PLAN failed: {explain_result.get('error', '')}",
                "plan_text": "",
                "metadata": {},
                "analysis": "",
            }

        # 2. Get the plan output
        plan_result = self.db_client.execute_query(_ORA_DISPLAY_PLAN)
        plan_lines = []
        if "error" not in plan_result:
            for row in plan_result.get("rows", []):
                line = row.get("plan_table_output", "")
                plan_lines.append(line)
        plan_text = "\n".join(plan_lines)
        sections["execution_plan"] = plan_text

        # 3. Extract tables from plan and collect metadata
        tables_result = self.db_client.execute_query(_ORA_EXTRACT_TABLES)
        tables = []
        if "error" not in tables_result:
            tables = tables_result.get("rows", [])

        metadata_parts: list[str] = []
        for tbl in tables[:10]:
            owner = tbl.get("owner", "")
            table_name = tbl.get("table_name", "")
            if not owner or not table_name:
                continue

            # Table columns + stats
            col_result = self.db_client.execute_query(
                _ORA_TABLE_DDL.format(owner=owner, table_name=table_name)
            )
            if "error" not in col_result and col_result.get("rows"):
                metadata_parts.append(f"\nTABLE: {owner}.{table_name} COLUMNS:")
                for r in col_result["rows"]:
                    metadata_parts.append(f"  {_fmt_row(r)}")

            # Indexes
            idx_result = self.db_client.execute_query(
                _ORA_TABLE_INDEXES.format(owner=owner, table_name=table_name)
            )
            if "error" not in idx_result and idx_result.get("rows"):
                metadata_parts.append(f"\nINDEXES ON {owner}.{table_name}:")
                for r in idx_result["rows"]:
                    metadata_parts.append(f"  {_fmt_row(r)}")

            # Table stats
            stat_result = self.db_client.execute_query(
                _ORA_TABLE_STATS.format(owner=owner, table_name=table_name)
            )
            if "error" not in stat_result and stat_result.get("rows"):
                metadata_parts.append(f"\nSTATISTICS FOR {owner}.{table_name}:")
                for r in stat_result["rows"]:
                    metadata_parts.append(f"  {_fmt_row(r)}")

        sections["table_metadata"] = "\n".join(metadata_parts)

        # 4. Build prompt and get LLM analysis
        prompt = self._build_prompt(sql, sections)
        analysis = self._get_llm_analysis(prompt)

        return {
            "plan_text": plan_text,
            "metadata": sections,
            "analysis": analysis,
        }

    # -- PostgreSQL -----------------------------------------------------------

    def _analyse_postgresql(
        self, sql: str, run_analyze: bool = False
    ) -> dict[str, Any]:
        sections: dict[str, str] = {}

        # 1. Run EXPLAIN
        if run_analyze:
            explain_sql = _PG_EXPLAIN_ANALYZE.format(sql=sql)
        else:
            explain_sql = _PG_EXPLAIN.format(sql=sql)

        plan_result = self.db_client.execute_query(explain_sql)
        if "error" in plan_result:
            return {
                "error": f"EXPLAIN failed: {plan_result['error']}",
                "plan_text": "",
                "metadata": {},
                "analysis": "",
            }

        plan_lines = []
        for row in plan_result.get("rows", []):
            # PostgreSQL EXPLAIN returns a single column
            line = list(row.values())[0] if row else ""
            plan_lines.append(str(line))
        plan_text = "\n".join(plan_lines)
        sections["execution_plan"] = plan_text

        # 2. Extract table names from the SQL (simple heuristic)
        tables = self._extract_pg_tables(sql)

        # 3. Collect metadata for each table
        metadata_parts: list[str] = []
        for schema, table_name in tables[:10]:
            # Columns
            col_result = self.db_client.execute_query(
                _PG_TABLE_COLUMNS.format(schema=schema, table_name=table_name)
            )
            if "error" not in col_result and col_result.get("rows"):
                metadata_parts.append(f"\nTABLE: {schema}.{table_name} COLUMNS:")
                for r in col_result["rows"]:
                    metadata_parts.append(f"  {_fmt_row(r)}")

            # Indexes
            idx_result = self.db_client.execute_query(
                _PG_TABLE_INDEXES.format(schema=schema, table_name=table_name)
            )
            if "error" not in idx_result and idx_result.get("rows"):
                metadata_parts.append(f"\nINDEXES ON {schema}.{table_name}:")
                for r in idx_result["rows"]:
                    metadata_parts.append(f"  {_fmt_row(r)}")

            # Table stats
            stat_result = self.db_client.execute_query(
                _PG_TABLE_STATS.format(schema=schema, table_name=table_name)
            )
            if "error" not in stat_result and stat_result.get("rows"):
                metadata_parts.append(f"\nTABLE STATS FOR {schema}.{table_name}:")
                for r in stat_result["rows"]:
                    metadata_parts.append(f"  {_fmt_row(r)}")

            # Column stats
            cstat_result = self.db_client.execute_query(
                _PG_COLUMN_STATS.format(schema=schema, table_name=table_name)
            )
            if "error" not in cstat_result and cstat_result.get("rows"):
                metadata_parts.append(f"\nCOLUMN STATS FOR {schema}.{table_name}:")
                for r in cstat_result["rows"]:
                    metadata_parts.append(f"  {_fmt_row(r)}")

        sections["table_metadata"] = "\n".join(metadata_parts)

        # 4. Build prompt and get LLM analysis
        prompt = self._build_prompt(sql, sections)
        analysis = self._get_llm_analysis(prompt)

        return {
            "plan_text": plan_text,
            "metadata": sections,
            "analysis": analysis,
        }

    def _extract_pg_tables(self, sql: str) -> list[tuple[str, str]]:
        """Extract table names from SQL using simple keyword parsing.

        Returns list of (schema, table_name) tuples.
        """
        import re

        tables: list[tuple[str, str]] = []
        seen: set[str] = set()

        # Match FROM/JOIN followed by optional schema.table
        pattern = r"(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_.]*)"
        for match in re.finditer(pattern, sql, re.IGNORECASE):
            full_name = match.group(1).strip().lower()
            # Skip subquery aliases and keywords
            if full_name in ("select", "where", "lateral", "unnest"):
                continue
            if full_name in seen:
                continue
            seen.add(full_name)

            if "." in full_name:
                schema, table = full_name.rsplit(".", 1)
            else:
                schema, table = "public", full_name
            tables.append((schema, table))

        return tables

    # -- Shared helpers -------------------------------------------------------

    def _build_prompt(self, sql: str, sections: dict[str, str]) -> str:
        parts = [
            f"SQL STATEMENT TO TUNE:\n```sql\n{sql}\n```\n",
            f"\nEXECUTION PLAN:\n```\n{sections.get('execution_plan', '(not available)')}\n```\n",
        ]
        meta = sections.get("table_metadata", "")
        if meta:
            parts.append(f"\nTABLE METADATA (columns, indexes, statistics):\n{meta}\n")

        return "\n".join(parts)

    def _get_llm_analysis(self, prompt: str) -> str:
        try:
            return self.llm_client.generate(
                prompt=prompt,
                system_prompt=TUNING_SYSTEM_PROMPT,
            )
        except (ConnectionError, RuntimeError) as exc:
            return f"LLM analysis failed: {exc}"


def _fmt_row(row: dict[str, Any]) -> str:
    """Format a row dict into a compact string."""
    items = []
    for k, v in row.items():
        if v is None:
            continue
        items.append(f"{k}={v}")
    return ", ".join(items)
