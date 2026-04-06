"""Compare two database snapshots with visual charts.

Supports Oracle AWR snap-ID ranges and PostgreSQL pgProfile sample-ID ranges.
Produces Plotly figures for side-by-side comparison of key metrics.
"""

import logging
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots

from db_client import BaseDBClient, DB_TYPE_ORACLE
from llm_client import LLMClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Oracle AWR delta queries (parameterised with :begin_snap / :end_snap)
# ---------------------------------------------------------------------------
_ORA_SNAP_TOP_SQL = """
    SELECT * FROM (
        SELECT
            s.sql_id,
            SUM(s.elapsed_time_delta) / 1e6 AS elapsed_sec,
            SUM(s.cpu_time_delta) / 1e6 AS cpu_sec,
            SUM(s.executions_delta) AS executions,
            SUM(s.buffer_gets_delta) AS buffer_gets,
            SUM(s.disk_reads_delta) AS disk_reads,
            SUM(s.rows_processed_delta) AS rows_processed
        FROM dba_hist_sqlstat s
        WHERE s.snap_id BETWEEN {begin_snap} AND {end_snap}
          AND s.parsing_schema_name NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
              'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
              'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
          )
        GROUP BY s.sql_id
        ORDER BY elapsed_sec DESC
    ) WHERE ROWNUM <= 20
"""

_ORA_SNAP_WAIT_EVENTS = """
    SELECT * FROM (
        SELECT
            event_name AS event,
            wait_class,
            SUM(total_waits_fg) AS total_waits,
            ROUND(SUM(time_waited_micro_fg) / 1e6, 2) AS time_waited_sec
        FROM dba_hist_system_event
        WHERE snap_id BETWEEN {begin_snap} AND {end_snap}
          AND wait_class != 'Idle'
        GROUP BY event_name, wait_class
        ORDER BY time_waited_sec DESC
    ) WHERE ROWNUM <= 15
"""

_ORA_SNAP_SYS_STATS = """
    SELECT
        stat_name AS name,
        SUM(value) AS value
    FROM dba_hist_sysstat
    WHERE snap_id BETWEEN {begin_snap} AND {end_snap}
      AND stat_name IN (
        'db block gets', 'consistent gets', 'physical reads',
        'redo size', 'sorts (memory)', 'sorts (disk)',
        'rows processed', 'parse count (total)', 'parse count (hard)',
        'execute count', 'user commits', 'user rollbacks',
        'enqueue waits', 'enqueue timeouts'
    )
    GROUP BY stat_name
    ORDER BY stat_name
"""

_ORA_SNAP_TOP_ELAPSED = """
    SELECT * FROM (
        SELECT
            s.sql_id,
            ROUND(SUM(s.elapsed_time_delta) / GREATEST(SUM(s.executions_delta), 1) / 1e6, 4)
                AS avg_elapsed_sec,
            SUM(s.executions_delta) AS executions,
            SUM(s.buffer_gets_delta) AS buffer_gets
        FROM dba_hist_sqlstat s
        WHERE s.snap_id BETWEEN {begin_snap} AND {end_snap}
          AND s.parsing_schema_name NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
              'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
              'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
          )
        GROUP BY s.sql_id
        HAVING SUM(s.executions_delta) > 0
        ORDER BY avg_elapsed_sec DESC
    ) WHERE ROWNUM <= 15
"""

# ---------------------------------------------------------------------------
# PostgreSQL pgProfile delta queries (parameterised with {begin_sample}/{end_sample})
# ---------------------------------------------------------------------------
_PG_SNAP_TOP_SQL = """
    SELECT
        sl.queryid::text AS queryid,
        SUM(ss.exec_time) / 1000.0 AS elapsed_sec,
        SUM(ss.calls) AS executions,
        SUM(ss.shared_blks_hit) AS shared_blks_hit,
        SUM(ss.shared_blks_read) AS shared_blks_read,
        SUM(ss.rows) AS rows_processed
    FROM profile.stmt_list sl
    JOIN profile.sample_statements ss ON sl.queryid_md5 = ss.queryid_md5
    WHERE ss.sample_id BETWEEN {begin_sample} AND {end_sample}
    GROUP BY sl.queryid
    ORDER BY elapsed_sec DESC
    LIMIT 20
"""

_PG_SNAP_WAIT_EVENTS = """
    SELECT
        event_type,
        event,
        SUM(tot_waited)::numeric AS time_waited_sec,
        SUM(tot_waits) AS total_waits
    FROM profile.wait_sampling_total
    WHERE sample_id BETWEEN {begin_sample} AND {end_sample}
    GROUP BY event_type, event
    ORDER BY time_waited_sec DESC
    LIMIT 15
"""

# PostgreSQL pg_stat_statements cumulative (no snap range - latest snapshot)
_PG_STAT_TOP_SQL = """
    SELECT
        queryid::text AS queryid,
        LEFT(query, 120) AS query_text,
        ROUND((total_exec_time / 1000)::numeric, 2) AS elapsed_sec,
        calls AS executions,
        shared_blks_hit,
        shared_blks_read,
        rows AS rows_processed,
        ROUND((mean_exec_time / 1000)::numeric, 4) AS avg_elapsed_sec
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND queryid IS NOT NULL
      AND query NOT LIKE 'SET %%'
      AND query NOT LIKE 'RESET %%'
      AND query NOT LIKE 'BEGIN%%'
      AND query NOT LIKE 'COMMIT%%'
      AND query NOT LIKE 'ROLLBACK%%'
    ORDER BY total_exec_time DESC
    LIMIT 20
"""

_PG_DB_STATS = """
    SELECT
        xact_commit, xact_rollback,
        blks_read, blks_hit,
        tup_returned, tup_fetched,
        tup_inserted, tup_updated, tup_deleted,
        temp_files, temp_bytes
    FROM pg_stat_database
    WHERE datname = current_database()
"""


# ---------------------------------------------------------------------------
# Comparison engine
# ---------------------------------------------------------------------------
class SnapshotComparator:
    """Compare two snapshot ranges and produce delta metrics + Plotly charts."""

    def __init__(self, db_client: BaseDBClient, llm_client: LLMClient) -> None:
        self.db = db_client
        self.llm = llm_client
        self.is_oracle = db_client.db_type == DB_TYPE_ORACLE

    # -- public API ----------------------------------------------------------

    def compare_oracle(
        self,
        snap_a_begin: int,
        snap_a_end: int,
        snap_b_begin: int,
        snap_b_end: int,
    ) -> dict[str, Any]:
        """Compare two AWR snap-ID ranges and return metrics + figures."""
        data_a = self._collect_oracle_snap(snap_a_begin, snap_a_end)
        data_b = self._collect_oracle_snap(snap_b_begin, snap_b_end)
        label_a = f"Snap {snap_a_begin}\u2013{snap_a_end}"
        label_b = f"Snap {snap_b_begin}\u2013{snap_b_end}"
        return self._build_comparison(data_a, data_b, label_a, label_b)

    def compare_pgprofile(
        self,
        samp_a_begin: int,
        samp_a_end: int,
        samp_b_begin: int,
        samp_b_end: int,
    ) -> dict[str, Any]:
        """Compare two pgProfile sample-ID ranges."""
        data_a = self._collect_pg_snap(samp_a_begin, samp_a_end)
        data_b = self._collect_pg_snap(samp_b_begin, samp_b_end)
        label_a = f"Sample {samp_a_begin}\u2013{samp_a_end}"
        label_b = f"Sample {samp_b_begin}\u2013{samp_b_end}"
        return self._build_comparison(data_a, data_b, label_a, label_b)

    # -- data collection -----------------------------------------------------

    def _run_query(self, sql: str) -> list[dict[str, Any]]:
        result = self.db.execute_query(sql)
        if "error" in result:
            logger.warning("Query error: %s", result["error"])
            return []
        return result.get("rows", [])

    def _collect_oracle_snap(self, begin: int, end: int) -> dict[str, Any]:
        fmt = {"begin_snap": str(begin), "end_snap": str(end)}
        return {
            "top_sql": self._run_query(_ORA_SNAP_TOP_SQL.format(**fmt)),
            "top_elapsed": self._run_query(_ORA_SNAP_TOP_ELAPSED.format(**fmt)),
            "wait_events": self._run_query(_ORA_SNAP_WAIT_EVENTS.format(**fmt)),
            "sys_stats": self._run_query(_ORA_SNAP_SYS_STATS.format(**fmt)),
            "snap_range": f"{begin}-{end}",
        }

    def _collect_pg_snap(self, begin: int, end: int) -> dict[str, Any]:
        fmt = {"begin_sample": str(begin), "end_sample": str(end)}
        return {
            "top_sql": self._run_query(_PG_SNAP_TOP_SQL.format(**fmt)),
            "wait_events": self._run_query(_PG_SNAP_WAIT_EVENTS.format(**fmt)),
            "snap_range": f"{begin}-{end}",
        }

    # -- comparison logic ----------------------------------------------------

    def _build_comparison(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> dict[str, Any]:
        figures: list[dict[str, Any]] = []

        # 1) Top SQL by elapsed time - grouped bar chart
        fig_sql = self._chart_top_sql_elapsed(data_a, data_b, label_a, label_b)
        if fig_sql:
            figures.append({"title": "Top SQL by Elapsed Time", "fig": fig_sql})

        # 2) Top SQL by executions - grouped bar chart
        fig_exec = self._chart_top_sql_executions(data_a, data_b, label_a, label_b)
        if fig_exec:
            figures.append({"title": "Top SQL by Executions", "fig": fig_exec})

        # 3) Top SQL by buffer gets - grouped bar chart
        fig_buf = self._chart_top_sql_buffer_gets(data_a, data_b, label_a, label_b)
        if fig_buf:
            figures.append({"title": "Top SQL by Buffer Gets", "fig": fig_buf})

        # 4) Wait events comparison - grouped bar chart
        fig_waits = self._chart_wait_events(data_a, data_b, label_a, label_b)
        if fig_waits:
            figures.append({"title": "Wait Events Comparison", "fig": fig_waits})

        # 5) Wait events by class/type - pie charts side by side
        fig_pie = self._chart_wait_pie(data_a, data_b, label_a, label_b)
        if fig_pie:
            figures.append({"title": "Wait Time Distribution", "fig": fig_pie})

        # 6) System stats comparison (Oracle only)
        if self.is_oracle:
            fig_sys = self._chart_sys_stats(data_a, data_b, label_a, label_b)
            if fig_sys:
                figures.append({"title": "System Statistics Delta", "fig": fig_sys})

        # 7) SQL elapsed per execution (Oracle only - has top_elapsed)
        if self.is_oracle:
            fig_avg = self._chart_avg_elapsed(data_a, data_b, label_a, label_b)
            if fig_avg:
                figures.append(
                    {"title": "Avg Elapsed per Execution (Top SQL)", "fig": fig_avg}
                )

        # Build delta summary table
        delta_table = self._build_delta_table(data_a, data_b, label_a, label_b)

        # LLM comparison summary
        comparison_text = self._format_comparison_text(
            data_a, data_b, label_a, label_b, delta_table
        )
        analysis = self._get_llm_comparison(comparison_text)

        return {
            "figures": figures,
            "delta_table": delta_table,
            "data_a": data_a,
            "data_b": data_b,
            "label_a": label_a,
            "label_b": label_b,
            "analysis": analysis,
        }

    # -- chart builders ------------------------------------------------------

    def _chart_top_sql_elapsed(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> go.Figure | None:
        sql_a = data_a.get("top_sql", [])
        sql_b = data_b.get("top_sql", [])
        if not sql_a and not sql_b:
            return None

        id_key = "sql_id" if self.is_oracle else "queryid"
        all_ids = []
        map_a: dict[str, float] = {}
        map_b: dict[str, float] = {}

        for row in sql_a[:10]:
            sid = str(row.get(id_key, ""))
            if sid:
                all_ids.append(sid)
                map_a[sid] = float(row.get("elapsed_sec", 0))
        for row in sql_b[:10]:
            sid = str(row.get(id_key, ""))
            if sid and sid not in all_ids:
                all_ids.append(sid)
            map_b[sid] = float(row.get("elapsed_sec", 0))

        if not all_ids:
            return None

        ids = all_ids[:12]
        short_ids = [s[:13] for s in ids]

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name=label_a,
                x=short_ids,
                y=[map_a.get(i, 0) for i in ids],
                marker_color="#636EFA",
            )
        )
        fig.add_trace(
            go.Bar(
                name=label_b,
                x=short_ids,
                y=[map_b.get(i, 0) for i in ids],
                marker_color="#EF553B",
            )
        )
        fig.update_layout(
            barmode="group",
            title="Top SQL \u2014 Elapsed Time (seconds)",
            xaxis_title="SQL ID" if self.is_oracle else "Query ID",
            yaxis_title="Elapsed (sec)",
            height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        return fig

    def _chart_top_sql_executions(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> go.Figure | None:
        sql_a = data_a.get("top_sql", [])
        sql_b = data_b.get("top_sql", [])
        if not sql_a and not sql_b:
            return None

        id_key = "sql_id" if self.is_oracle else "queryid"
        all_ids: list[str] = []
        map_a: dict[str, float] = {}
        map_b: dict[str, float] = {}

        for row in sql_a[:10]:
            sid = str(row.get(id_key, ""))
            if sid:
                all_ids.append(sid)
                map_a[sid] = float(row.get("executions", 0))
        for row in sql_b[:10]:
            sid = str(row.get(id_key, ""))
            if sid and sid not in all_ids:
                all_ids.append(sid)
            map_b[sid] = float(row.get("executions", 0))

        if not all_ids:
            return None

        ids = all_ids[:12]
        short_ids = [s[:13] for s in ids]

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name=label_a,
                x=short_ids,
                y=[map_a.get(i, 0) for i in ids],
                marker_color="#636EFA",
            )
        )
        fig.add_trace(
            go.Bar(
                name=label_b,
                x=short_ids,
                y=[map_b.get(i, 0) for i in ids],
                marker_color="#EF553B",
            )
        )
        fig.update_layout(
            barmode="group",
            title="Top SQL \u2014 Executions",
            xaxis_title="SQL ID" if self.is_oracle else "Query ID",
            yaxis_title="Executions",
            height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        return fig

    def _chart_top_sql_buffer_gets(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> go.Figure | None:
        sql_a = data_a.get("top_sql", [])
        sql_b = data_b.get("top_sql", [])
        if not sql_a and not sql_b:
            return None

        id_key = "sql_id" if self.is_oracle else "queryid"
        buf_key = "buffer_gets" if self.is_oracle else "shared_blks_hit"
        all_ids: list[str] = []
        map_a: dict[str, float] = {}
        map_b: dict[str, float] = {}

        for row in sql_a[:10]:
            sid = str(row.get(id_key, ""))
            if sid:
                all_ids.append(sid)
                map_a[sid] = float(row.get(buf_key, 0))
        for row in sql_b[:10]:
            sid = str(row.get(id_key, ""))
            if sid and sid not in all_ids:
                all_ids.append(sid)
            map_b[sid] = float(row.get(buf_key, 0))

        if not all_ids:
            return None

        ids = all_ids[:12]
        short_ids = [s[:13] for s in ids]

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name=label_a,
                x=short_ids,
                y=[map_a.get(i, 0) for i in ids],
                marker_color="#636EFA",
            )
        )
        fig.add_trace(
            go.Bar(
                name=label_b,
                x=short_ids,
                y=[map_b.get(i, 0) for i in ids],
                marker_color="#EF553B",
            )
        )
        fig.update_layout(
            barmode="group",
            title=f"Top SQL \u2014 {'Buffer Gets' if self.is_oracle else 'Shared Blocks Hit'}",
            xaxis_title="SQL ID" if self.is_oracle else "Query ID",
            yaxis_title="Buffer Gets" if self.is_oracle else "Shared Blocks Hit",
            height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        return fig

    def _chart_wait_events(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> go.Figure | None:
        wa = data_a.get("wait_events", [])
        wb = data_b.get("wait_events", [])
        if not wa and not wb:
            return None

        all_events: list[str] = []
        map_a: dict[str, float] = {}
        map_b: dict[str, float] = {}

        for row in wa[:10]:
            evt = str(row.get("event", ""))
            if evt:
                all_events.append(evt)
                map_a[evt] = float(row.get("time_waited_sec", 0))
        for row in wb[:10]:
            evt = str(row.get("event", ""))
            if evt and evt not in all_events:
                all_events.append(evt)
            map_b[evt] = float(row.get("time_waited_sec", 0))

        if not all_events:
            return None

        events = all_events[:12]
        short_events = [e[:30] for e in events]

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name=label_a,
                x=short_events,
                y=[map_a.get(e, 0) for e in events],
                marker_color="#636EFA",
            )
        )
        fig.add_trace(
            go.Bar(
                name=label_b,
                x=short_events,
                y=[map_b.get(e, 0) for e in events],
                marker_color="#EF553B",
            )
        )
        fig.update_layout(
            barmode="group",
            title="Wait Events \u2014 Time Waited (seconds)",
            xaxis_title="Event",
            yaxis_title="Time Waited (sec)",
            height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        return fig

    def _chart_wait_pie(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> go.Figure | None:
        wa = data_a.get("wait_events", [])
        wb = data_b.get("wait_events", [])
        if not wa and not wb:
            return None

        class_key = "wait_class" if self.is_oracle else "event_type"

        def aggregate_by_class(rows: list[dict]) -> tuple[list[str], list[float]]:
            agg: dict[str, float] = {}
            for row in rows:
                cls = str(row.get(class_key, "Other"))
                agg[cls] = agg.get(cls, 0) + float(row.get("time_waited_sec", 0))
            labels = list(agg.keys())
            values = list(agg.values())
            return labels, values

        labels_a, values_a = aggregate_by_class(wa)
        labels_b, values_b = aggregate_by_class(wb)

        if not values_a and not values_b:
            return None

        fig = make_subplots(
            rows=1,
            cols=2,
            specs=[[{"type": "pie"}, {"type": "pie"}]],
            subplot_titles=[label_a, label_b],
        )
        if values_a:
            fig.add_trace(
                go.Pie(labels=labels_a, values=values_a, hole=0.35, name=label_a),
                row=1,
                col=1,
            )
        if values_b:
            fig.add_trace(
                go.Pie(labels=labels_b, values=values_b, hole=0.35, name=label_b),
                row=1,
                col=2,
            )
        fig.update_layout(
            title="Wait Time Distribution by Class",
            height=400,
        )
        return fig

    def _chart_sys_stats(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> go.Figure | None:
        sa = data_a.get("sys_stats", [])
        sb = data_b.get("sys_stats", [])
        if not sa and not sb:
            return None

        map_a: dict[str, float] = {}
        map_b: dict[str, float] = {}
        all_names: list[str] = []

        for row in sa:
            name = str(row.get("name", ""))
            if name:
                all_names.append(name)
                map_a[name] = float(row.get("value", 0))
        for row in sb:
            name = str(row.get("name", ""))
            if name and name not in all_names:
                all_names.append(name)
            map_b[name] = float(row.get("value", 0))

        if not all_names:
            return None

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name=label_a,
                x=all_names,
                y=[map_a.get(n, 0) for n in all_names],
                marker_color="#636EFA",
            )
        )
        fig.add_trace(
            go.Bar(
                name=label_b,
                x=all_names,
                y=[map_b.get(n, 0) for n in all_names],
                marker_color="#EF553B",
            )
        )
        fig.update_layout(
            barmode="group",
            title="System Statistics Comparison",
            xaxis_title="Statistic",
            yaxis_title="Value",
            height=450,
            xaxis_tickangle=-35,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        return fig

    def _chart_avg_elapsed(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> go.Figure | None:
        ea = data_a.get("top_elapsed", [])
        eb = data_b.get("top_elapsed", [])
        if not ea and not eb:
            return None

        all_ids: list[str] = []
        map_a: dict[str, float] = {}
        map_b: dict[str, float] = {}

        for row in ea[:10]:
            sid = str(row.get("sql_id", ""))
            if sid:
                all_ids.append(sid)
                map_a[sid] = float(row.get("avg_elapsed_sec", 0))
        for row in eb[:10]:
            sid = str(row.get("sql_id", ""))
            if sid and sid not in all_ids:
                all_ids.append(sid)
            map_b[sid] = float(row.get("avg_elapsed_sec", 0))

        if not all_ids:
            return None

        ids = all_ids[:12]
        short_ids = [s[:13] for s in ids]

        fig = go.Figure()
        fig.add_trace(
            go.Bar(
                name=label_a,
                x=short_ids,
                y=[map_a.get(i, 0) for i in ids],
                marker_color="#636EFA",
            )
        )
        fig.add_trace(
            go.Bar(
                name=label_b,
                x=short_ids,
                y=[map_b.get(i, 0) for i in ids],
                marker_color="#EF553B",
            )
        )
        fig.update_layout(
            barmode="group",
            title="Avg Elapsed per Execution (seconds)",
            xaxis_title="SQL ID",
            yaxis_title="Avg Elapsed (sec)",
            height=420,
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        return fig

    # -- delta summary table -------------------------------------------------

    def _build_delta_table(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
    ) -> list[dict[str, Any]]:
        """Build a summary table of key metric deltas between snapshots."""
        rows: list[dict[str, Any]] = []

        # Total elapsed time across top SQL
        total_a = sum(float(r.get("elapsed_sec", 0)) for r in data_a.get("top_sql", []))
        total_b = sum(float(r.get("elapsed_sec", 0)) for r in data_b.get("top_sql", []))
        rows.append(
            self._delta_row(
                "Total Top SQL Elapsed (sec)", total_a, total_b, label_a, label_b
            )
        )

        # Total executions across top SQL
        exec_a = sum(float(r.get("executions", 0)) for r in data_a.get("top_sql", []))
        exec_b = sum(float(r.get("executions", 0)) for r in data_b.get("top_sql", []))
        rows.append(
            self._delta_row(
                "Total Top SQL Executions", exec_a, exec_b, label_a, label_b
            )
        )

        # Total wait time
        wait_a = sum(
            float(r.get("time_waited_sec", 0)) for r in data_a.get("wait_events", [])
        )
        wait_b = sum(
            float(r.get("time_waited_sec", 0)) for r in data_b.get("wait_events", [])
        )
        rows.append(
            self._delta_row("Total Wait Time (sec)", wait_a, wait_b, label_a, label_b)
        )

        # Buffer gets / shared blocks
        buf_key = "buffer_gets" if self.is_oracle else "shared_blks_hit"
        buf_a = sum(float(r.get(buf_key, 0)) for r in data_a.get("top_sql", []))
        buf_b = sum(float(r.get(buf_key, 0)) for r in data_b.get("top_sql", []))
        buf_label = "Buffer Gets" if self.is_oracle else "Shared Blocks Hit"
        rows.append(
            self._delta_row(f"Total {buf_label}", buf_a, buf_b, label_a, label_b)
        )

        # Disk reads / shared blocks read
        disk_key = "disk_reads" if self.is_oracle else "shared_blks_read"
        disk_a = sum(float(r.get(disk_key, 0)) for r in data_a.get("top_sql", []))
        disk_b = sum(float(r.get(disk_key, 0)) for r in data_b.get("top_sql", []))
        disk_label = "Disk Reads" if self.is_oracle else "Shared Blocks Read"
        rows.append(
            self._delta_row(f"Total {disk_label}", disk_a, disk_b, label_a, label_b)
        )

        # Oracle-specific system stats
        if self.is_oracle:
            stats_a = {
                str(r.get("name", "")): float(r.get("value", 0))
                for r in data_a.get("sys_stats", [])
            }
            stats_b = {
                str(r.get("name", "")): float(r.get("value", 0))
                for r in data_b.get("sys_stats", [])
            }
            for stat_name in [
                "physical reads",
                "parse count (hard)",
                "execute count",
                "user commits",
                "enqueue waits",
            ]:
                va = stats_a.get(stat_name, 0)
                vb = stats_b.get(stat_name, 0)
                if va or vb:
                    rows.append(
                        self._delta_row(stat_name.title(), va, vb, label_a, label_b)
                    )

        return rows

    @staticmethod
    def _delta_row(
        metric: str, val_a: float, val_b: float, label_a: str, label_b: str
    ) -> dict[str, Any]:
        delta = val_b - val_a
        pct = (delta / val_a * 100) if val_a else 0
        direction = "+" if delta > 0 else ("-" if delta < 0 else "=")
        return {
            "metric": metric,
            label_a: round(val_a, 2),
            label_b: round(val_b, 2),
            "delta": round(delta, 2),
            "change_pct": f"{direction}{abs(pct):.1f}%",
        }

    # -- LLM comparison analysis ---------------------------------------------

    def _format_comparison_text(
        self,
        data_a: dict[str, Any],
        data_b: dict[str, Any],
        label_a: str,
        label_b: str,
        delta_table: list[dict[str, Any]],
    ) -> str:
        parts = [
            f"SNAPSHOT COMPARISON REPORT\n{'=' * 60}",
            f"Snapshot A: {label_a}",
            f"Snapshot B: {label_b}\n",
            "--- DELTA SUMMARY ---",
        ]
        for row in delta_table:
            parts.append(
                f"  {row['metric']}: {row[label_a]} -> {row[label_b]} "
                f"(delta={row['delta']}, {row['change_pct']})"
            )

        parts.append("\n--- SNAPSHOT A: TOP SQL ---")
        for i, row in enumerate(data_a.get("top_sql", [])[:10], 1):
            parts.append(f"  [{i}] {_fmt(row)}")

        parts.append("\n--- SNAPSHOT B: TOP SQL ---")
        for i, row in enumerate(data_b.get("top_sql", [])[:10], 1):
            parts.append(f"  [{i}] {_fmt(row)}")

        parts.append("\n--- SNAPSHOT A: WAIT EVENTS ---")
        for i, row in enumerate(data_a.get("wait_events", [])[:10], 1):
            parts.append(f"  [{i}] {_fmt(row)}")

        parts.append("\n--- SNAPSHOT B: WAIT EVENTS ---")
        for i, row in enumerate(data_b.get("wait_events", [])[:10], 1):
            parts.append(f"  [{i}] {_fmt(row)}")

        return "\n".join(parts)

    def _get_llm_comparison(self, text: str) -> str:
        system_prompt = (
            "You are a senior DBA comparing two database performance snapshots. "
            "Produce a detailed comparison report with these sections:\n\n"
            "## Executive Summary\n"
            "2-3 sentences on overall change in database health between the two periods.\n\n"
            "## Key Metric Changes\n"
            "For each metric that changed significantly (>10%), explain the change "
            "and its likely cause. Reference specific sql_id/queryid values.\n\n"
            "## New or Regressed SQL\n"
            "Identify SQL that appeared in Snapshot B but not A (new workload), or SQL "
            "whose elapsed time increased significantly. For each, explain the likely "
            "cause and provide specific fix SQL (CREATE INDEX, ANALYZE, rewrite).\n\n"
            "## Wait Event Changes\n"
            "Highlight wait events that increased or decreased. Explain implications "
            "(e.g., increased 'enq: TX - row lock contention' suggests locking issues).\n\n"
            "## Recommendations\n"
            "Numbered action plan sorted by impact. Each item must include:\n"
            "- The specific sql_id/queryid/object affected\n"
            "- The exact SQL command to execute\n"
            "- Expected improvement\n\n"
            "IMPORTANT: Be SPECIFIC. Always reference sql_id, queryid, or table names. "
            "Never give generic advice. Use markdown code blocks for SQL."
        )
        try:
            return self.llm.generate(prompt=text, system_prompt=system_prompt)
        except (ConnectionError, RuntimeError) as exc:
            return f"LLM comparison analysis failed: {exc}"


def _fmt(row: dict[str, Any]) -> str:
    """Format a row dict compactly."""
    return ", ".join(f"{k}={v}" for k, v in row.items() if v is not None)
