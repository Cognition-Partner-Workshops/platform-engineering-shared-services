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
            SUBSTR(sql_fulltext, 1, 500) AS sql_text
        FROM v$sql
        WHERE parsing_schema_name NOT IN (
            'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
            'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
            'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
        )
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

_ORA_FULL_TABLE_SCANS = """
    SELECT * FROM (
        SELECT
            p.sql_id,
            p.plan_hash_value,
            p.object_owner,
            p.object_name AS table_name,
            p.operation || ' ' || NVL(p.options, '') AS operation,
            s.executions,
            ROUND(s.elapsed_time / 1e6, 2) AS elapsed_sec,
            s.buffer_gets,
            s.disk_reads,
            SUBSTR(s.sql_fulltext, 1, 500) AS sql_text
        FROM v$sql_plan p
        JOIN v$sql s ON p.sql_id = s.sql_id
            AND p.child_number = s.child_number
        WHERE p.operation = 'TABLE ACCESS'
            AND p.options = 'FULL'
            AND p.object_owner NOT IN (
                'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
                'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
                'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
            )
            AND s.parsing_schema_name NOT IN (
                'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
                'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
                'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
            )
        ORDER BY s.elapsed_time DESC
    ) WHERE ROWNUM <= 20
"""

_ORA_TOP_CPU_SQL = """
    SELECT * FROM (
        SELECT
            sql_id,
            plan_hash_value,
            ROUND(cpu_time / 1e6, 2) AS cpu_sec,
            ROUND(elapsed_time / 1e6, 2) AS elapsed_sec,
            executions,
            buffer_gets,
            ROUND(buffer_gets / GREATEST(executions, 1)) AS gets_per_exec,
            SUBSTR(sql_fulltext, 1, 500) AS sql_text
        FROM v$sql
        WHERE cpu_time > 0
          AND parsing_schema_name NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
              'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
              'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
          )
        ORDER BY cpu_time DESC
    ) WHERE ROWNUM <= 15
"""

_ORA_EXISTING_INDEXES = """
    SELECT
        i.table_name,
        i.index_name,
        i.index_type,
        i.uniqueness,
        i.status,
        i.num_rows AS index_rows,
        i.last_analyzed,
        LISTAGG(c.column_name, ', ') WITHIN GROUP (ORDER BY c.column_position) AS columns
    FROM all_indexes i
    JOIN all_ind_columns c ON i.index_name = c.index_name AND i.owner = c.index_owner
    WHERE i.owner NOT IN ('SYS', 'SYSTEM', 'DBSNMP', 'OUTLN', 'XDB', 'WMSYS')
        AND i.table_owner NOT IN ('SYS', 'SYSTEM', 'DBSNMP', 'OUTLN', 'XDB', 'WMSYS')
    GROUP BY i.table_name, i.index_name, i.index_type, i.uniqueness,
             i.status, i.num_rows, i.last_analyzed
    ORDER BY i.table_name, i.index_name
"""

_ORA_STALE_STATS = """
    SELECT
        table_name,
        num_rows,
        TO_CHAR(last_analyzed, 'YYYY-MM-DD HH24:MI') AS last_analyzed,
        stale_stats,
        ROUND((SYSDATE - last_analyzed), 1) AS days_since_analyzed
    FROM all_tab_statistics
    WHERE owner NOT IN ('SYS', 'SYSTEM', 'DBSNMP', 'OUTLN', 'XDB', 'WMSYS')
        AND (stale_stats = 'YES' OR last_analyzed IS NULL
             OR last_analyzed < SYSDATE - 7)
    ORDER BY CASE WHEN last_analyzed IS NULL THEN 0
                  ELSE last_analyzed END
"""

_ORA_SQL_PLAN_DETAIL = """
    SELECT
        sql_id,
        plan_hash_value,
        id AS step_id,
        LPAD(' ', 2 * depth) || operation || ' ' || NVL(options, '') AS operation,
        object_name,
        ROUND(cost) AS cost,
        cardinality AS est_rows,
        bytes AS est_bytes,
        access_predicates,
        filter_predicates
    FROM v$sql_plan
    WHERE sql_id = '{sql_id}'
    ORDER BY child_number, id
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
            DBMS_LOB.SUBSTR(t.sql_text, 500, 1) AS sql_text
        FROM dba_hist_sqlstat s
        JOIN dba_hist_sqltext t ON s.sql_id = t.sql_id AND s.dbid = t.dbid
        WHERE s.snap_id BETWEEN :begin_snap AND :end_snap
          AND s.parsing_schema_name NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
              'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
              'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
          )
        GROUP BY s.sql_id, s.plan_hash_value,
                 DBMS_LOB.SUBSTR(t.sql_text, 500, 1)
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
        LEFT(query, 500) AS query_text,
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

# PostgreSQL < 17: checkpoint columns live in pg_stat_bgwriter.
# PostgreSQL >= 17: they moved to pg_stat_checkpointer with renamed columns.
_PG_BGWRITER_LEGACY = """
    SELECT
        checkpoints_timed, checkpoints_req,
        buffers_checkpoint, buffers_clean, buffers_backend,
        maxwritten_clean
    FROM pg_stat_bgwriter
"""

_PG_BGWRITER_V17 = """
    SELECT
        num_timed AS checkpoints_timed,
        num_requested AS checkpoints_req,
        buffers_written AS buffers_checkpoint,
        bg.buffers_clean,
        bg.buffers_alloc AS buffers_backend,
        bg.maxwritten_clean
    FROM pg_stat_checkpointer cp
    CROSS JOIN pg_stat_bgwriter bg
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

_PG_SEQ_SCAN_TABLES = """
    SELECT
        schemaname, relname,
        seq_scan,
        seq_tup_read,
        COALESCE(idx_scan, 0) AS idx_scan,
        n_live_tup,
        CASE WHEN seq_scan > 0 AND n_live_tup > 0
            THEN ROUND(seq_tup_read::numeric / GREATEST(seq_scan, 1))
            ELSE 0
        END AS avg_rows_per_seq_scan,
        pg_relation_size(relid) / 1048576 AS table_size_mb
    FROM pg_stat_user_tables
    WHERE seq_scan > 0
        AND n_live_tup > 1000
    ORDER BY seq_tup_read DESC
    LIMIT 20
"""

_PG_EXISTING_INDEXES = """
    SELECT
        schemaname, tablename, indexname,
        indexdef
    FROM pg_indexes
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY tablename, indexname
"""

_PG_STALE_STATS = """
    SELECT
        schemaname, relname,
        n_live_tup,
        n_dead_tup,
        CASE WHEN n_live_tup > 0
            THEN ROUND(n_dead_tup::numeric / n_live_tup * 100, 2)
            ELSE 0
        END AS dead_pct,
        last_vacuum::text,
        last_autovacuum::text,
        last_analyze::text,
        last_autoanalyze::text
    FROM pg_stat_user_tables
    WHERE n_dead_tup > 1000
        OR last_analyze IS NULL
        OR last_analyze < now() - interval '7 days'
    ORDER BY n_dead_tup DESC
    LIMIT 30
"""

_PG_TOP_CPU_QUERIES = """
    SELECT
        queryid,
        LEFT(query, 500) AS query_text,
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
        END AS cache_hit_pct,
        ROUND((blk_read_time / 1000)::numeric, 2) AS blk_read_sec,
        ROUND((blk_write_time / 1000)::numeric, 2) AS blk_write_sec,
        temp_blks_read,
        temp_blks_written
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND queryid IS NOT NULL
      AND query NOT LIKE 'SET %%'
      AND query NOT LIKE 'RESET %%'
      AND query NOT LIKE 'BEGIN%%'
      AND query NOT LIKE 'COMMIT%%'
      AND query NOT LIKE 'ROLLBACK%%'
    ORDER BY total_exec_time DESC
    LIMIT 15
"""

_PG_LOCK_WAITS = """
    SELECT
        pid,
        usename,
        LEFT(query, 500) AS query,
        wait_event_type,
        wait_event,
        state,
        ROUND(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 2) AS running_sec
    FROM pg_stat_activity
    WHERE state != 'idle'
        AND wait_event IS NOT NULL
    ORDER BY query_start
    LIMIT 20
"""

# ---------------------------------------------------------------------------
# Oracle best-practice checks
# ---------------------------------------------------------------------------
_ORA_ROW_CONTENTION = """
    SELECT * FROM (
        SELECT
            event,
            total_waits,
            ROUND(time_waited / 100, 2) AS time_waited_sec,
            ROUND(average_wait / 100, 4) AS avg_wait_sec
        FROM v$system_event
        WHERE event IN (
            'enq: TX - row lock contention',
            'enq: TX - index contention',
            'enq: TX - allocate ITL entry',
            'enq: TM - contention',
            'enq: HW - contention',
            'buffer busy waits',
            'gc buffer busy acquire',
            'gc buffer busy release',
            'row cache lock',
            'library cache lock',
            'cursor: pin S wait on X'
        )
        ORDER BY time_waited DESC
    ) WHERE ROWNUM <= 20
"""

_ORA_SEQUENCE_NO_CACHE = """
    SELECT
        sequence_owner,
        sequence_name,
        min_value,
        max_value,
        increment_by,
        cache_size,
        order_flag,
        cycle_flag,
        last_number
    FROM all_sequences
    WHERE sequence_owner NOT IN (
        'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
        'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
        'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
    )
    AND (cache_size = 0 OR cache_size = 1)
    ORDER BY sequence_owner, sequence_name
"""

_ORA_HIGH_ELAPSED_PER_EXEC = """
    SELECT * FROM (
        SELECT
            sql_id,
            plan_hash_value,
            executions,
            ROUND(elapsed_time / GREATEST(executions, 1) / 1e6, 4)
                AS avg_elapsed_sec,
            ROUND(elapsed_time / 1e6, 2) AS total_elapsed_sec,
            buffer_gets,
            ROUND(buffer_gets / GREATEST(executions, 1)) AS gets_per_exec,
            SUBSTR(sql_fulltext, 1, 500) AS sql_text
        FROM v$sql
        WHERE executions > 0
          AND elapsed_time / GREATEST(executions, 1) / 1e6 > 1
          AND parsing_schema_name NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
              'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
              'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
          )
        ORDER BY avg_elapsed_sec DESC
    ) WHERE ROWNUM <= 15
"""

_ORA_HIGH_EXEC_COUNT = """
    SELECT * FROM (
        SELECT
            sql_id,
            plan_hash_value,
            executions,
            ROUND(elapsed_time / 1e6, 2) AS total_elapsed_sec,
            ROUND(cpu_time / 1e6, 2) AS total_cpu_sec,
            buffer_gets,
            ROUND(buffer_gets / GREATEST(executions, 1)) AS gets_per_exec,
            SUBSTR(sql_fulltext, 1, 500) AS sql_text
        FROM v$sql
        WHERE executions > 1000
          AND parsing_schema_name NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
              'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
              'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
          )
        ORDER BY executions DESC
    ) WHERE ROWNUM <= 15
"""

_ORA_REDO_LOG_SWITCHES = """
    SELECT * FROM (
        SELECT
            TO_CHAR(first_time, 'YYYY-MM-DD HH24') AS switch_hour,
            COUNT(*) AS switches
        FROM v$log_history
        WHERE first_time > SYSDATE - 1
        GROUP BY TO_CHAR(first_time, 'YYYY-MM-DD HH24')
        ORDER BY switch_hour DESC
    ) WHERE ROWNUM <= 24
"""

_ORA_TEMP_USAGE = """
    SELECT
        tablespace_name,
        ROUND(SUM(bytes_used) / 1048576, 2) AS used_mb,
        ROUND(SUM(bytes_free) / 1048576, 2) AS free_mb,
        ROUND(SUM(bytes_used) / (SUM(bytes_used) + SUM(bytes_free)) * 100, 2)
            AS pct_used
    FROM v$temp_space_header
    GROUP BY tablespace_name
    ORDER BY pct_used DESC
"""

_ORA_PARALLEL_QUERIES = """
    SELECT * FROM (
        SELECT
            sql_id,
            users_executing,
            px_servers_executions AS px_servers,
            ROUND(elapsed_time / 1e6, 2) AS elapsed_sec,
            SUBSTR(sql_fulltext, 1, 500) AS sql_text
        FROM v$sql
        WHERE px_servers_executions > 0
          AND parsing_schema_name NOT IN (
              'SYS','SYSTEM','DBSNMP','OUTLN','XDB','WMSYS',
              'CTXSYS','MDSYS','ORDSYS','ORDDATA','LBACSYS',
              'APEX_PUBLIC_USER','FLOWS_FILES','DVSYS','AUDSYS'
          )
        ORDER BY px_servers_executions DESC
    ) WHERE ROWNUM <= 10
"""

# ---------------------------------------------------------------------------
# PostgreSQL best-practice checks
# ---------------------------------------------------------------------------
_PG_HIGH_ELAPSED_PER_EXEC = """
    SELECT
        queryid,
        LEFT(query, 500) AS query_text,
        calls,
        ROUND((total_exec_time / calls / 1000)::numeric, 4) AS avg_elapsed_sec,
        ROUND((total_exec_time / 1000)::numeric, 2) AS total_exec_sec,
        rows,
        shared_blks_hit,
        shared_blks_read,
        temp_blks_read,
        temp_blks_written
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND calls > 0
      AND total_exec_time / calls / 1000 > 1
      AND queryid IS NOT NULL
      AND query NOT LIKE 'SET %%'
      AND query NOT LIKE 'RESET %%'
      AND query NOT LIKE 'BEGIN%%'
      AND query NOT LIKE 'COMMIT%%'
      AND query NOT LIKE 'ROLLBACK%%'
    ORDER BY avg_elapsed_sec DESC
    LIMIT 15
"""

_PG_HIGH_EXEC_COUNT = """
    SELECT
        queryid,
        LEFT(query, 500) AS query_text,
        calls,
        ROUND((total_exec_time / 1000)::numeric, 2) AS total_exec_sec,
        ROUND((mean_exec_time / 1000)::numeric, 4) AS mean_exec_sec,
        rows,
        shared_blks_hit + shared_blks_read AS total_blocks
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND calls > 1000
      AND queryid IS NOT NULL
      AND query NOT LIKE 'SET %%'
      AND query NOT LIKE 'RESET %%'
      AND query NOT LIKE 'BEGIN%%'
      AND query NOT LIKE 'COMMIT%%'
      AND query NOT LIKE 'ROLLBACK%%'
    ORDER BY calls DESC
    LIMIT 15
"""

_PG_BLOAT_ESTIMATE = """
    SELECT
        schemaname, relname,
        n_live_tup,
        n_dead_tup,
        CASE WHEN n_live_tup > 0
            THEN ROUND(n_dead_tup::numeric / n_live_tup * 100, 2)
            ELSE 0
        END AS dead_pct,
        pg_relation_size(relid) / 1048576 AS table_size_mb,
        last_autovacuum::text,
        last_autoanalyze::text
    FROM pg_stat_user_tables
    WHERE n_dead_tup > 10000
      OR (n_live_tup > 0 AND n_dead_tup::numeric / n_live_tup > 0.2)
    ORDER BY n_dead_tup DESC
    LIMIT 20
"""

_PG_SEQUENCE_CACHE = """
    SELECT
        schemaname,
        sequencename,
        start_value,
        min_value,
        max_value,
        increment_by,
        cache_size,
        cycle
    FROM pg_sequences
    WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
      AND (cache_size IS NULL OR cache_size <= 1)
    ORDER BY schemaname, sequencename
"""

_PG_TEMP_FILE_USAGE = """
    SELECT
        queryid,
        LEFT(query, 500) AS query_text,
        calls,
        temp_blks_read,
        temp_blks_written,
        ROUND((temp_blks_read + temp_blks_written) * 8.0 / 1024, 2)
            AS temp_mb,
        ROUND((total_exec_time / 1000)::numeric, 2) AS total_exec_sec
    FROM pg_stat_statements
    WHERE dbid = (SELECT oid FROM pg_database WHERE datname = current_database())
      AND (temp_blks_read > 0 OR temp_blks_written > 0)
      AND queryid IS NOT NULL
      AND query NOT LIKE 'SET %%'
      AND query NOT LIKE 'RESET %%'
      AND query NOT LIKE 'BEGIN%%'
    ORDER BY temp_blks_read + temp_blks_written DESC
    LIMIT 15
"""

_PG_CONNECTION_STATS = """
    SELECT
        state,
        COUNT(*) AS count,
        COALESCE(wait_event_type, 'None') AS wait_event_type
    FROM pg_stat_activity
    WHERE backend_type = 'client backend'
    GROUP BY state, wait_event_type
    ORDER BY count DESC
"""

_PG_CHECKPOINT_STATS_LEGACY = """
    SELECT
        checkpoints_timed,
        checkpoints_req,
        buffers_checkpoint,
        buffers_clean,
        buffers_backend,
        maxwritten_clean,
        ROUND(buffers_backend::numeric /
              GREATEST(buffers_checkpoint + buffers_clean + buffers_backend, 1)
              * 100, 2) AS backend_write_pct
    FROM pg_stat_bgwriter
"""

_PG_CHECKPOINT_STATS_V17 = """
    SELECT
        cp.num_timed AS checkpoints_timed,
        cp.num_requested AS checkpoints_req,
        cp.buffers_written AS buffers_checkpoint,
        bg.buffers_clean,
        bg.buffers_alloc AS buffers_backend,
        bg.maxwritten_clean,
        ROUND(bg.buffers_alloc::numeric /
              GREATEST(cp.buffers_written + bg.buffers_clean + bg.buffers_alloc, 1)
              * 100, 2) AS backend_write_pct
    FROM pg_stat_checkpointer cp
    CROSS JOIN pg_stat_bgwriter bg
"""

_PG_TABLE_SIZES = """
    SELECT
        schemaname,
        relname,
        pg_relation_size(relid) / 1048576 AS table_size_mb,
        pg_total_relation_size(relid) / 1048576 AS total_size_mb,
        (pg_total_relation_size(relid) - pg_relation_size(relid)) / 1048576
            AS toast_index_size_mb,
        n_live_tup,
        n_dead_tup,
        n_tup_ins, n_tup_upd, n_tup_del
    FROM pg_stat_user_tables
    ORDER BY pg_total_relation_size(relid) DESC
    LIMIT 20
"""

_PG_WAL_STATS = """
    SELECT
        wal_records,
        wal_fpi,
        wal_bytes,
        wal_buffers_full,
        wal_write,
        wal_sync,
        ROUND(wal_write_time::numeric, 2) AS wal_write_time_ms,
        ROUND(wal_sync_time::numeric, 2) AS wal_sync_time_ms,
        stats_reset::text AS stats_reset
    FROM pg_stat_wal
"""

_PG_IDLE_IN_TRANSACTION = """
    SELECT
        pid,
        usename,
        datname,
        state,
        LEFT(query, 300) AS query,
        ROUND(EXTRACT(EPOCH FROM (now() - state_change))::numeric, 0)
            AS idle_duration_sec,
        ROUND(EXTRACT(EPOCH FROM (now() - xact_start))::numeric, 0)
            AS xact_duration_sec
    FROM pg_stat_activity
    WHERE state = 'idle in transaction'
    ORDER BY xact_start
    LIMIT 20
"""

_PG_CONFIG_PARAMS = """
    SELECT name, setting, unit
    FROM pg_settings
    WHERE name IN (
        'shared_buffers', 'effective_cache_size', 'work_mem',
        'maintenance_work_mem', 'max_connections', 'max_wal_size',
        'min_wal_size', 'checkpoint_timeout', 'checkpoint_completion_target',
        'random_page_cost', 'effective_io_concurrency',
        'autovacuum_max_workers', 'autovacuum_vacuum_scale_factor',
        'autovacuum_analyze_scale_factor', 'statement_timeout',
        'idle_in_transaction_session_timeout', 'wal_level',
        'max_worker_processes', 'max_parallel_workers',
        'max_parallel_workers_per_gather', 'wal_compression',
        'huge_pages', 'shared_preload_libraries'
    )
    ORDER BY name
"""

_PG_REPLICATION_STATUS = """
    SELECT
        client_addr::text,
        state,
        sent_lsn::text,
        write_lsn::text,
        flush_lsn::text,
        replay_lsn::text,
        ROUND(EXTRACT(EPOCH FROM write_lag)::numeric, 3) AS write_lag_sec,
        ROUND(EXTRACT(EPOCH FROM flush_lag)::numeric, 3) AS flush_lag_sec,
        ROUND(EXTRACT(EPOCH FROM replay_lag)::numeric, 3) AS replay_lag_sec
    FROM pg_stat_replication
"""

_ORA_CONFIG_PARAMS = """
    SELECT name, value, description
    FROM v$parameter
    WHERE name IN (
        'sga_target', 'sga_max_size', 'pga_aggregate_target',
        'db_cache_size', 'shared_pool_size', 'log_buffer',
        'processes', 'sessions', 'open_cursors',
        'cursor_sharing', 'optimizer_mode', 'db_file_multiblock_read_count',
        'undo_retention', 'undo_tablespace',
        'result_cache_max_size', 'parallel_max_servers',
        'parallel_min_servers', 'job_queue_processes'
    )
    ORDER BY name
"""

_ORA_IDLE_SESSIONS = """
    SELECT * FROM (
        SELECT
            sid,
            serial#,
            username,
            status,
            machine,
            program,
            ROUND(last_call_et / 60, 1) AS idle_minutes,
            sql_id AS last_sql_id
        FROM v$session
        WHERE status = 'INACTIVE'
          AND type = 'USER'
          AND last_call_et > 300
        ORDER BY last_call_et DESC
    ) WHERE ROWNUM <= 20
"""

# ---------------------------------------------------------------------------
# Programmatic analysis — Python code does the heavy lifting, not the LLM.
# ---------------------------------------------------------------------------


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert a value to float."""
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _safe_int(val: Any, default: int = 0) -> int:
    """Safely convert a value to int."""
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _truncate_sql(sql_text: str, length: int = 200) -> str:
    """Truncate SQL text for display."""
    if not sql_text:
        return "(no SQL text)"
    sql_text = str(sql_text).strip()
    if len(sql_text) > length:
        return sql_text[:length] + "..."
    return sql_text


def _fmt_bytes(b: float) -> str:
    """Format bytes into human-readable size."""
    if b >= 1073741824:
        return f"{b / 1073741824:.1f} GB"
    if b >= 1048576:
        return f"{b / 1048576:.1f} MB"
    if b >= 1024:
        return f"{b / 1024:.1f} KB"
    return f"{b:.0f} B"


def _fmt_secs(s: float) -> str:
    """Format seconds into human-readable duration."""
    if s >= 86400:
        return f"{s / 86400:.1f} days"
    if s >= 3600:
        return f"{s / 3600:.1f} hrs"
    if s >= 60:
        return f"{s / 60:.1f} min"
    return f"{s:.2f} sec"


def _build_findings_report(data: dict[str, Any]) -> str:
    """Production-grade performance analysis report.

    Analyses collected data programmatically — identifies bottlenecks,
    groups by severity, references specific SQL IDs / table names / metrics.
    No LLM is involved. Output format inspired by enterprise DBA assessments.
    """
    db_type = data.get("db_type", "unknown")
    is_oracle = db_type == DB_TYPE_ORACLE
    # Accumulate bottlenecks as (severity, title, details_markdown)
    bottlenecks: list[tuple[int, str, str]] = []
    # Accumulate prioritised actions as (priority, action_text)
    actions: list[tuple[int, str]] = []
    # Accumulate risks as (likelihood, impact, description)
    risks: list[tuple[str, str, str]] = []
    act_idx = 0

    # =====================================================================
    # PHASE 1 — Extract key metrics
    # =====================================================================

    # --- PostgreSQL database stats ---
    cache_hit = 100.0
    commits = rollbacks = 0
    backends = 0
    temp_bytes = temp_files = 0
    blks_read = blks_hit = 0
    if not is_oracle:
        db_rows = _get_rows(data, "database_stats")
        if db_rows:
            row = db_rows[0]
            cache_hit = _safe_float(row.get("cache_hit_pct", 100))
            commits = _safe_int(row.get("xact_commit", 0))
            rollbacks = _safe_int(row.get("xact_rollback", 0))
            backends = _safe_int(row.get("numbackends", 0))
            blks_read = _safe_int(row.get("blks_read", 0))
            blks_hit = _safe_int(row.get("blks_hit", 0))
            temp_bytes = _safe_int(row.get("temp_bytes", 0))
            temp_files = _safe_int(row.get("temp_files", 0))

    # --- Oracle system stats ---
    ora_cache_hit = 100.0
    ora_hard_parse_pct = 0.0
    ora_disk_sort_pct = 0.0
    ora_rb_count = 0
    ora_commit_count = 0
    if is_oracle:
        sys_rows = _get_rows(data, "system_stats") or _get_rows(
            data, "awr_system_stats"
        )
        stats_map: dict[str, int] = {}
        for row in sys_rows:
            stats_map[str(row.get("name", ""))] = _safe_int(row.get("value", 0))
        db_gets = stats_map.get("db block gets", 0)
        consistent = stats_map.get("consistent gets", 0)
        phys_reads = stats_map.get("physical reads", 0)
        logical = db_gets + consistent
        if logical > 0:
            ora_cache_hit = (1 - phys_reads / logical) * 100
        hard_parse = stats_map.get("parse count (hard)", 0)
        total_parse = stats_map.get("parse count (total)", 0)
        if total_parse > 0:
            ora_hard_parse_pct = hard_parse / total_parse * 100
        sorts_disk = stats_map.get("sorts (disk)", 0)
        sorts_mem = stats_map.get("sorts (memory)", 0)
        if sorts_mem + sorts_disk > 0:
            ora_disk_sort_pct = sorts_disk / (sorts_mem + sorts_disk) * 100
        ora_rb_count = stats_map.get("user rollbacks", 0)
        ora_commit_count = stats_map.get("user commits", 0)

    # --- WAL stats (PostgreSQL 14+) ---
    wal_bytes = 0
    wal_fpi = 0
    wal_sync_time_ms = 0.0
    wal_write_time_ms = 0.0
    if not is_oracle:
        wal_rows = _get_rows(data, "wal_stats")
        if wal_rows:
            w = wal_rows[0]
            wal_bytes = _safe_int(w.get("wal_bytes", 0))
            wal_fpi = _safe_int(w.get("wal_fpi", 0))
            wal_sync_time_ms = _safe_float(w.get("wal_sync_time_ms", 0))
            wal_write_time_ms = _safe_float(w.get("wal_write_time_ms", 0))

    # --- Connection counts ---
    idle_in_tx_rows = _get_rows(data, "idle_in_transaction") if not is_oracle else []
    idle_session_rows = _get_rows(data, "idle_sessions") if is_oracle else []
    conn_rows = _get_rows(data, "connection_stats") if not is_oracle else []
    idle_count = sum(
        _safe_int(r.get("count", 0))
        for r in conn_rows
        if (r.get("state") or "").startswith("idle")
    )

    # --- Top SQL ---
    top_cpu = (
        _get_rows(data, "top_cpu_sql")
        if is_oracle
        else _get_rows(data, "top_cpu_queries")
    )
    top_elapsed = (
        _get_rows(data, "top_elapsed_sql")
        if is_oracle
        else _get_rows(data, "top_queries")
    )
    # Fallback to AWR / pgProfile
    if not top_cpu and not top_elapsed:
        top_elapsed = _get_rows(data, "awr_top_sql") or _get_rows(
            data, "pgprofile_top_sql"
        )
    high_elapsed = _get_rows(data, "high_elapsed_per_exec")
    high_exec = _get_rows(data, "high_execution_count")
    fts = (
        _get_rows(data, "full_table_scans")
        if is_oracle
        else _get_rows(data, "seq_scan_tables")
    )

    # --- Tables ---
    table_sizes = _get_rows(data, "table_sizes") if not is_oracle else []
    bloat_rows = _get_rows(data, "bloat_estimate") if not is_oracle else []
    unused_idx = _get_rows(data, "unused_indexes")
    stale_rows = (
        _get_rows(data, "stale_stats_vacuum")
        if not is_oracle
        else _get_rows(data, "stale_statistics")
    )

    # --- Contention ---
    contention = (
        _get_rows(data, "row_contention")
        if is_oracle
        else _get_rows(data, "lock_waits")
    )
    wait_rows = (
        _get_rows(data, "wait_events")
        or _get_rows(data, "awr_wait_events")
        or _get_rows(data, "pgprofile_wait_events")
    )

    # --- Sequences ---
    seqs = (
        _get_rows(data, "sequence_no_cache")
        if is_oracle
        else _get_rows(data, "sequence_cache_issues")
    )

    # --- Config ---
    config_rows = _get_rows(data, "config_params")

    # --- Checkpoint (PG) ---
    ckpt_rows = _get_rows(data, "checkpoint_stats") if not is_oracle else []

    # --- Replication (PG) ---
    repl_rows = _get_rows(data, "replication_status") if not is_oracle else []

    # --- Temp file usage (PG) ---
    temp_sql_rows = _get_rows(data, "temp_file_usage") if not is_oracle else []

    # =====================================================================
    # PHASE 2 -- Identify bottlenecks with severity
    # =====================================================================
    # Severity 1 = critical, 2 = important, 3 = advisory

    # -- Rollback explosion --
    rb_rate = 0.0
    if not is_oracle and commits + rollbacks > 0:
        rb_rate = rollbacks / (commits + rollbacks) * 100
    elif is_oracle and ora_commit_count + ora_rb_count > 0:
        rb_rate = ora_rb_count / (ora_commit_count + ora_rb_count) * 100
    if rb_rate > 10:
        detail = (
            f"**{rollbacks:,} rollbacks** vs {commits:,} commits "
            f"(**{rb_rate:.1f}% rollback rate**)\n\n"
            if not is_oracle
            else f"**{ora_rb_count:,} rollbacks** vs {ora_commit_count:,} commits "
            f"(**{rb_rate:.1f}% rollback rate**)\n\n"
        )
        detail += (
            "This almost always means:\n"
            "- Business validation aborts\n"
            "- Exception-based flow control\n"
            "- Retry loops without guardrails\n\n"
            "Directly increases WAL, dead tuples, autovacuum load."
        )
        bottlenecks.append((1, "Rollback Explosion", detail))
        act_idx += 1
        actions.append(
            (
                0,
                f"{act_idx}. **Root-cause rollbacks** -- identify "
                f"why {rb_rate:.1f}% of transactions are aborted",
            )
        )
        risks.append(("High", "Severe", "Dead tuple accumulation from rollbacks"))
    elif rb_rate > 5:
        total_rb = rollbacks if not is_oracle else ora_rb_count
        bottlenecks.append(
            (
                2,
                "Elevated Rollback Rate",
                f"Rollback rate is **{rb_rate:.1f}%** "
                f"({total_rb:,} rollbacks). Investigate application "
                f"error handling.",
            )
        )
        act_idx += 1
        actions.append((1, f"{act_idx}. Investigate rollback sources"))

    # -- Idle-in-transaction (PG) --
    if idle_in_tx_rows:
        total_idle_sec = sum(
            _safe_float(r.get("xact_duration_sec", 0)) for r in idle_in_tx_rows
        )
        longest = max(
            _safe_float(r.get("xact_duration_sec", 0)) for r in idle_in_tx_rows
        )
        detail = (
            f"**{len(idle_in_tx_rows)} sessions** idle in transaction, "
            f"cumulative **{_fmt_secs(total_idle_sec)}**, "
            f"longest **{_fmt_secs(longest)}**\n\n"
            "| PID | User | Duration | Query |\n"
            "| --- | --- | --- | --- |\n"
        )
        for r in idle_in_tx_rows[:10]:
            pid = r.get("pid", "?")
            user = r.get("usename", "?")
            dur = _fmt_secs(_safe_float(r.get("xact_duration_sec", 0)))
            q = _truncate_sql(str(r.get("query", "")), 80)
            detail += f"| {pid} | {user} | {dur} | `{q}` |\n"
        detail += (
            "\nImpact: prevents vacuum, creates dead tuples, "
            "increases lock contention.\n"
            "This is an **application defect**, not a DB tuning issue."
        )
        sev = 1 if total_idle_sec > 3600 else 2
        bottlenecks.append((sev, "Idle-in-Transaction Sessions", detail))
        act_idx += 1
        actions.append(
            (
                0,
                f"{act_idx}. **Fix idle-in-transaction at app layer** -- "
                f"enforce connection/transaction guards, "
                f"set `idle_in_transaction_session_timeout`",
            )
        )
        risks.append(("High", "Severe", "Bloat & lock risk from idle-in-tx"))

    # -- Idle Oracle sessions --
    if idle_session_rows and len(idle_session_rows) > 5:
        detail = (
            f"**{len(idle_session_rows)} sessions** idle > 5 minutes\n\n"
            "| SID | User | Idle (min) | Program |\n"
            "| --- | --- | --- | --- |\n"
        )
        for r in idle_session_rows[:10]:
            detail += (
                f"| {r.get('sid', '?')} | {r.get('username', '?')} "
                f"| {_safe_float(r.get('idle_minutes', 0)):.0f} "
                f"| {r.get('program', '?')} |\n"
            )
        bottlenecks.append((2, "Excessive Idle Sessions", detail))
        act_idx += 1
        actions.append(
            (
                1,
                f"{act_idx}. Review idle sessions -- consider "
                f"connection pooling or session timeout",
            )
        )

    # -- Cache hit ratio --
    eff_cache_hit = cache_hit if not is_oracle else ora_cache_hit
    if eff_cache_hit < 95:
        detail = f"Buffer cache hit ratio: **{eff_cache_hit:.2f}%** (target > 99%)\n\n"
        if not is_oracle:
            detail += (
                f"Blocks hit: {blks_hit:,}, blocks read from disk: {blks_read:,}\n\n"
                f"**Fix:** Increase `shared_buffers` "
                f"(current value shown in Configuration Review below)."
            )
        else:
            detail += "**Fix:** Increase `db_cache_size`."
        sev = 1 if eff_cache_hit < 90 else 2
        bottlenecks.append((sev, "Low Buffer Cache Hit Ratio", detail))
        act_idx += 1
        param = "shared_buffers" if not is_oracle else "db_cache_size"
        actions.append(
            (
                0,
                f"{act_idx}. **Increase `{param}`** -- cache hit is {eff_cache_hit:.2f}%",
            )
        )
        risks.append(("High", "High", "Excessive disk I/O from cache misses"))

    # -- WAL pressure (PG) --
    if wal_bytes > 0:
        wal_gb = wal_bytes / 1073741824
        detail = (
            f"**{wal_gb:.1f} GB WAL** generated (since stats reset)\n"
            f"- Full-page images (FPI): {wal_fpi:,}\n"
            f"- WAL sync time: {wal_sync_time_ms / 1000:.1f} sec\n"
            f"- WAL write time: {wal_write_time_ms / 1000:.1f} sec\n"
        )
        if wal_sync_time_ms > wal_write_time_ms * 5 and wal_sync_time_ms > 1000:
            detail += (
                "\nWAL sync time is **much higher** than write time "
                "-- disk sync latency issue."
            )
            bottlenecks.append((1, "WAL & Write Pressure", detail))
            risks.append(("Medium-High", "Severe", "WAL disk saturation"))
        elif wal_gb > 10:
            bottlenecks.append((2, "High WAL Volume", detail))
        act_idx += 1
        actions.append(
            (
                1,
                f"{act_idx}. Review WAL generation -- "
                f"batch commits, consider `wal_compression`",
            )
        )

    # -- Hard parse ratio (Oracle) --
    if is_oracle and ora_hard_parse_pct > 30:
        bottlenecks.append(
            (
                2,
                "High Hard Parse Ratio",
                f"Hard parse ratio: **{ora_hard_parse_pct:.1f}%**\n\n"
                f"**Fix:** Use bind variables instead of literal values.",
            )
        )
        act_idx += 1
        actions.append(
            (
                1,
                f"{act_idx}. Use bind variables -- "
                f"hard parse ratio is {ora_hard_parse_pct:.1f}%",
            )
        )

    # -- Disk sorts (Oracle) --
    if is_oracle and ora_disk_sort_pct > 5:
        bottlenecks.append(
            (
                2,
                "Disk Sorts",
                f"**{ora_disk_sort_pct:.1f}%** of sorts go to disk.\n\n"
                f"**Fix:** Increase `PGA_AGGREGATE_TARGET` or `SORT_AREA_SIZE`.",
            )
        )
        act_idx += 1
        actions.append(
            (1, f"{act_idx}. Increase PGA -- {ora_disk_sort_pct:.1f}% disk sorts")
        )

    # -- Top SQL bottlenecks --
    top_sql_all = top_cpu or top_elapsed
    if top_sql_all:
        total_elapsed = sum(
            _safe_float(
                r.get("elapsed_sec", 0)
                or r.get("total_exec_sec", 0)
                or r.get("cpu_sec", 0)
            )
            for r in top_sql_all
        )
        top1 = top_sql_all[0]
        if is_oracle:
            t1_id = top1.get("sql_id", "?")
            t1_elapsed = _safe_float(
                top1.get("elapsed_sec", 0) or top1.get("cpu_sec", 0)
            )
            t1_execs = _safe_int(top1.get("executions", 0))
            t1_gets = _safe_int(top1.get("buffer_gets", 0))
            t1_sql = str(top1.get("sql_text", ""))
        else:
            t1_id = str(top1.get("queryid", "?"))
            t1_elapsed = _safe_float(top1.get("total_exec_sec", 0))
            t1_execs = _safe_int(top1.get("calls", 0))
            t1_gets = _safe_int(top1.get("shared_blks_hit", 0)) + _safe_int(
                top1.get("shared_blks_read", 0)
            )
            t1_sql = str(top1.get("query_text", ""))

        detail = (
            f"Top query alone: **{_fmt_secs(t1_elapsed)}** elapsed, "
            f"**{t1_execs:,}** executions, **{t1_gets:,}** buffer gets\n\n"
        )
        id_col = "sql_id" if is_oracle else "queryid"
        detail += (
            f"| # | {id_col} | Elapsed | Executions | Buffer Gets | Query |\n"
            f"| --- | --- | --- | --- | --- | --- |\n"
        )
        for i, r in enumerate(top_sql_all[:10]):
            if is_oracle:
                sid = r.get("sql_id", "?")
                elapsed = _safe_float(r.get("elapsed_sec", 0) or r.get("cpu_sec", 0))
                execs = _safe_int(r.get("executions", 0))
                gets = _safe_int(r.get("buffer_gets", 0))
                sql = _truncate_sql(str(r.get("sql_text", "")), 60)
            else:
                sid = str(r.get("queryid", "?"))
                elapsed = _safe_float(r.get("total_exec_sec", 0))
                execs = _safe_int(r.get("calls", 0))
                gets = _safe_int(r.get("shared_blks_hit", 0)) + _safe_int(
                    r.get("shared_blks_read", 0)
                )
                sql = _truncate_sql(str(r.get("query_text", "")), 60)
            detail += (
                f"| {i + 1} | `{sid}` | {_fmt_secs(elapsed)} "
                f"| {execs:,} | {gets:,} | `{sql}` |\n"
            )

        if t1_sql:
            detail += (
                f"\n**Top #1 full query text:**\n"
                f"```sql\n{_truncate_sql(t1_sql, 500)}\n```\n"
            )

        if len(top_sql_all) >= 3:
            top3_elapsed = sum(
                _safe_float(
                    r.get("elapsed_sec", 0)
                    or r.get("total_exec_sec", 0)
                    or r.get("cpu_sec", 0)
                )
                for r in top_sql_all[:3]
            )
            if total_elapsed > 0 and top3_elapsed / total_elapsed > 0.7:
                pct = top3_elapsed / total_elapsed * 100
                detail += f"\n**Top 3 queries = ~{pct:.0f}% of total execution time.**"

        bottlenecks.append((1, "Query-Level Offenders (Top SQL)", detail))
        act_idx += 1
        actions.append(
            (
                0,
                f"{act_idx}. **Review top SQL** -- "
                f"{id_col} `{t1_id}` accounts for "
                f"{_fmt_secs(t1_elapsed)} elapsed",
            )
        )

    # -- High elapsed per execution --
    if high_elapsed:
        detail = "Queries taking > 1 sec per execution:\n\n"
        id_col = "sql_id" if is_oracle else "queryid"
        detail += (
            f"| {id_col} | Avg Elapsed | Total Elapsed | Execs | Query |\n"
            f"| --- | --- | --- | --- | --- |\n"
        )
        for r in high_elapsed[:10]:
            if is_oracle:
                sid = r.get("sql_id", "?")
                avg_e = _safe_float(r.get("avg_elapsed_sec", 0))
                tot_e = _safe_float(r.get("total_elapsed_sec", 0))
                execs = _safe_int(r.get("executions", 0))
                sql = _truncate_sql(str(r.get("sql_text", "")), 60)
            else:
                sid = str(r.get("queryid", "?"))
                avg_e = _safe_float(r.get("avg_elapsed_sec", 0))
                tot_e = _safe_float(r.get("total_exec_sec", 0))
                execs = _safe_int(r.get("calls", 0))
                sql = _truncate_sql(str(r.get("query_text", "")), 60)
            detail += (
                f"| `{sid}` | {_fmt_secs(avg_e)} | {_fmt_secs(tot_e)} "
                f"| {execs:,} | `{sql}` |\n"
            )
        bottlenecks.append((2, "High Elapsed Time per Execution", detail))
        act_idx += 1
        actions.append(
            (
                1,
                f"{act_idx}. Tune slow queries -- "
                f"{len(high_elapsed)} queries > 1 sec/exec",
            )
        )

    # -- High execution count --
    if high_exec:
        detail = "Queries with > 1,000 executions (high frequency):\n\n"
        id_col = "sql_id" if is_oracle else "queryid"
        detail += (
            f"| {id_col} | Calls | Total Elapsed | Avg Elapsed | Query |\n"
            f"| --- | --- | --- | --- | --- |\n"
        )
        for r in high_exec[:10]:
            if is_oracle:
                sid = r.get("sql_id", "?")
                execs = _safe_int(r.get("executions", 0))
                tot_e = _safe_float(r.get("total_elapsed_sec", 0))
                avg_e = tot_e / max(execs, 1)
                sql = _truncate_sql(str(r.get("sql_text", "")), 60)
            else:
                sid = str(r.get("queryid", "?"))
                execs = _safe_int(r.get("calls", 0))
                tot_e = _safe_float(r.get("total_exec_sec", 0))
                avg_e = _safe_float(r.get("mean_exec_sec", 0))
                sql = _truncate_sql(str(r.get("query_text", "")), 60)
            detail += (
                f"| `{sid}` | {execs:,} | {_fmt_secs(tot_e)} "
                f"| {_fmt_secs(avg_e)} | `{sql}` |\n"
            )
        bottlenecks.append((2, "High Execution Count Queries", detail))

    # -- Full table scans / Sequential scans --
    if fts:
        if is_oracle:
            detail = "Full table scans detected:\n\n"
            detail += (
                "| sql_id | Table | Executions | Elapsed | Query |\n"
                "| --- | --- | --- | --- | --- |\n"
            )
            for r in fts[:10]:
                owner = r.get("object_owner", "")
                table = r.get("table_name", "?")
                sid = r.get("sql_id", "?")
                execs = _safe_int(r.get("executions", 0))
                elapsed = _safe_float(r.get("elapsed_sec", 0))
                sql = _truncate_sql(str(r.get("sql_text", "")), 60)
                detail += (
                    f"| `{sid}` | `{owner}.{table}` | {execs:,} "
                    f"| {_fmt_secs(elapsed)} | `{sql}` |\n"
                )
                act_idx += 1
                actions.append(
                    (
                        1,
                        f"{act_idx}. **Add index** on `{owner}.{table}` "
                        f"for sql_id `{sid}` (full table scan, "
                        f"{execs:,} execs, {_fmt_secs(elapsed)})",
                    )
                )
        else:
            detail = "Tables with heavy sequential scans:\n\n"
            detail += (
                "| Table | Seq Scans | Rows/Scan | Size | "
                "Idx Scans | Live Rows |\n"
                "| --- | --- | --- | --- | --- | --- |\n"
            )
            for r in fts[:10]:
                schema = r.get("schemaname", "public")
                table = r.get("relname", "?")
                ss = _safe_int(r.get("seq_scan", 0))
                avg_r = _safe_int(r.get("avg_rows_per_seq_scan", 0))
                sz = _safe_float(r.get("table_size_mb", 0))
                idx_s = _safe_int(r.get("idx_scan", 0))
                live = _safe_int(r.get("n_live_tup", 0))
                detail += (
                    f"| `{schema}.{table}` | {ss:,} | {avg_r:,} "
                    f"| {sz:.1f} MB | {idx_s:,} | {live:,} |\n"
                )
                if ss > 100 and live > 10000:
                    act_idx += 1
                    actions.append(
                        (
                            1,
                            f"{act_idx}. **Add index** on `{schema}.{table}` -- "
                            f"{ss:,} seq scans on {live:,} rows ({sz:.1f} MB)",
                        )
                    )
        sev = 1 if len(fts) > 5 else 2
        bottlenecks.append((sev, "Full Table Scans / Sequential Scans", detail))
        risks.append(("Medium-High", "High", "I/O amplification from table scans"))

    # -- Contention & locking --
    if contention:
        if is_oracle:
            detail = "Contention/lock wait events:\n\n"
            detail += (
                "| Event | Waits | Time Waited | Avg Wait |\n"
                "| --- | --- | --- | --- |\n"
            )
            for r in contention[:10]:
                event = r.get("event", "?")
                waits = _safe_int(r.get("total_waits", 0))
                tw = _safe_float(r.get("time_waited_sec", 0))
                aw = _safe_float(r.get("avg_wait_sec", 0))
                detail += (
                    f"| {event} | {waits:,} | {_fmt_secs(tw)} | {_fmt_secs(aw)} |\n"
                )
        else:
            detail = "Active lock waits:\n\n"
            detail += (
                "| PID | User | Wait Event | Running | Query |\n"
                "| --- | --- | --- | --- | --- |\n"
            )
            for r in contention[:10]:
                pid = r.get("pid", "?")
                user = r.get("usename", "?")
                we = f"{r.get('wait_event_type', '')}:{r.get('wait_event', '')}"
                dur = _safe_float(r.get("running_sec", 0))
                q = _truncate_sql(str(r.get("query", "")), 60)
                detail += f"| {pid} | {user} | {we} | {_fmt_secs(dur)} | `{q}` |\n"
        bottlenecks.append((2, "Row Contention & Locking", detail))
        risks.append(("Medium", "High", "Lock escalation / deadlock risk"))

    # -- Wait events --
    if wait_rows and not contention:
        detail = "Top wait events:\n\n"
        detail += "| Event | Waits | Time Waited |\n| --- | --- | --- |\n"
        for r in wait_rows[:10]:
            event = r.get("event", r.get("event_name", "?"))
            waits = _safe_int(r.get("total_waits", 0))
            tw = _safe_float(r.get("time_waited_sec", 0))
            detail += f"| {event} | {waits:,} | {_fmt_secs(tw)} |\n"
        bottlenecks.append((2, "Top Wait Events", detail))

    # -- Table sizes & bloat (PG) --
    if table_sizes:
        detail = "Largest tables:\n\n"
        detail += (
            "| Table | Total Size | Table Size | TOAST+Idx | "
            "Live Rows | Ins | Upd | Del |\n"
            "| --- | --- | --- | --- | --- | --- | --- | --- |\n"
        )
        for r in table_sizes[:10]:
            schema = r.get("schemaname", "public")
            table = r.get("relname", "?")
            total = _safe_float(r.get("total_size_mb", 0))
            tbl = _safe_float(r.get("table_size_mb", 0))
            toast = _safe_float(r.get("toast_index_size_mb", 0))
            live = _safe_int(r.get("n_live_tup", 0))
            ins = _safe_int(r.get("n_tup_ins", 0))
            upd = _safe_int(r.get("n_tup_upd", 0))
            dele = _safe_int(r.get("n_tup_del", 0))
            total_str = f"{total / 1024:.1f} GB" if total >= 1024 else f"{total:.0f} MB"
            tbl_str = f"{tbl / 1024:.1f} GB" if tbl >= 1024 else f"{tbl:.0f} MB"
            toast_str = f"{toast / 1024:.1f} GB" if toast >= 1024 else f"{toast:.0f} MB"
            detail += (
                f"| `{schema}.{table}` | {total_str} | {tbl_str} "
                f"| {toast_str} | {live:,} | {ins:,} | {upd:,} | {dele:,} |\n"
            )
            if total > 10240:
                risks.append(
                    (
                        "Medium",
                        "Medium",
                        f"`{schema}.{table}` is {total_str} -- consider partitioning",
                    )
                )
                act_idx += 1
                actions.append(
                    (
                        1,
                        f"{act_idx}. **Partition** `{schema}.{table}` "
                        f"({total_str}) -- time-based or business key",
                    )
                )
            if toast > tbl and toast > 1024:
                act_idx += 1
                actions.append(
                    (
                        2,
                        f"{act_idx}. Review TOAST usage on `{schema}.{table}` "
                        f"-- TOAST+Idx ({toast_str}) > table ({tbl_str})",
                    )
                )
        bottlenecks.append((2, "Table Sizes & Storage", detail))

    # -- Bloat (PG) --
    if bloat_rows:
        high_bloat = [r for r in bloat_rows if _safe_float(r.get("dead_pct", 0)) > 20]
        if high_bloat:
            detail = "Tables with significant bloat (dead tuples > 20%):\n\n"
            detail += (
                "| Table | Dead % | Dead Tuples | Size | Last Vacuum |\n"
                "| --- | --- | --- | --- | --- |\n"
            )
            for r in high_bloat[:10]:
                schema = r.get("schemaname", "public")
                table = r.get("relname", "?")
                dp = _safe_float(r.get("dead_pct", 0))
                dead = _safe_int(r.get("n_dead_tup", 0))
                sz = _safe_float(r.get("table_size_mb", 0))
                lv = r.get("last_autovacuum", "never") or "never"
                detail += (
                    f"| `{schema}.{table}` | {dp:.1f}% | {dead:,} "
                    f"| {sz:.0f} MB | {lv} |\n"
                )
                act_idx += 1
                actions.append(
                    (
                        1,
                        f"{act_idx}. **VACUUM FULL** `{schema}.{table}` -- "
                        f"{dp:.1f}% dead tuples ({dead:,}): "
                        f"`VACUUM (VERBOSE, ANALYZE) {schema}.{table};`",
                    )
                )
            bottlenecks.append((1, "Table Bloat", detail))
            risks.append(("High", "Severe", "Disk exhaustion from bloat"))

    # -- Stale statistics / missing vacuum --
    stale_critical: list[dict[str, Any]] = []
    if stale_rows:
        for r in stale_rows:
            if is_oracle:
                stale = r.get("stale_stats", "")
                days = _safe_float(r.get("days_since_analyzed", 0))
                if stale == "YES" or days > 7:
                    stale_critical.append(r)
            else:
                dead_pct = _safe_float(r.get("dead_pct", 0))
                la = r.get("last_analyze") or r.get("last_autoanalyze")
                if dead_pct > 10 or not la:
                    stale_critical.append(r)
    if stale_critical:
        detail = "Tables with stale/missing statistics:\n\n"
        if is_oracle:
            detail += (
                "| Table | Rows | Last Analyzed | Days Stale |\n"
                "| --- | --- | --- | --- |\n"
            )
            for r in stale_critical[:15]:
                table = r.get("table_name", "?")
                rows = _safe_int(r.get("num_rows", 0))
                la = r.get("last_analyzed", "never")
                days = _safe_float(r.get("days_since_analyzed", 0))
                detail += f"| `{table}` | {rows:,} | {la} | {days:.0f} |\n"
                act_idx += 1
                actions.append(
                    (
                        2,
                        f"{act_idx}. `EXEC DBMS_STATS.GATHER_TABLE_STATS"
                        f"(ownname=>USER, tabname=>'{table}');`",
                    )
                )
        else:
            detail += (
                "| Table | Dead % | Dead Tuples | Last Analyze |\n"
                "| --- | --- | --- | --- |\n"
            )
            for r in stale_critical[:15]:
                schema = r.get("schemaname", "public")
                table = r.get("relname", "?")
                dp = _safe_float(r.get("dead_pct", 0))
                dead = _safe_int(r.get("n_dead_tup", 0))
                la = r.get("last_analyze") or r.get("last_autoanalyze") or "never"
                detail += f"| `{schema}.{table}` | {dp:.1f}% | {dead:,} | {la} |\n"
                act_idx += 1
                actions.append((2, f"{act_idx}. `ANALYZE {schema}.{table};`"))
        bottlenecks.append((2, "Stale Statistics / Missing Vacuum", detail))

    # -- Unused indexes --
    if unused_idx:
        total_waste_mb = sum(_safe_float(r.get("index_size_mb", 0)) for r in unused_idx)
        detail = (
            f"**{len(unused_idx)} unused indexes** "
            f"consuming **{total_waste_mb:.0f} MB**:\n\n"
            "| Index | Table | Size |\n"
            "| --- | --- | --- |\n"
        )
        for r in unused_idx[:15]:
            if is_oracle:
                idx = r.get("index_name", "?")
                table = r.get("table_name", "?")
                sz = _safe_float(r.get("index_rows", 0))
                detail += f"| `{idx}` | `{table}` | {sz:,} rows |\n"
            else:
                schema = r.get("schemaname", "public")
                idx = r.get("indexrelname", "?")
                table = r.get("relname", "?")
                sz = _safe_float(r.get("index_size_mb", 0))
                detail += f"| `{schema}.{idx}` | `{table}` | {sz:.0f} MB |\n"
                act_idx += 1
                actions.append(
                    (
                        2,
                        f"{act_idx}. `DROP INDEX {schema}.{idx};` "
                        f"-- never used, {sz:.0f} MB",
                    )
                )
        bottlenecks.append((3, "Unused Indexes", detail))

    # -- Sequence caching --
    if seqs:
        detail = "Sequences with no/low caching (cache_size <= 1):\n\n"
        detail += "| Sequence | Cache Size |\n| --- | --- |\n"
        for r in seqs[:15]:
            if is_oracle:
                name = f"{r.get('sequence_owner', '')}.{r.get('sequence_name', '?')}"
                cache = _safe_int(r.get("cache_size", 0))
            else:
                name = f"{r.get('schemaname', 'public')}.{r.get('sequencename', '?')}"
                cache = _safe_int(r.get("cache_size", 0))
            detail += f"| `{name}` | {cache} |\n"
        detail += (
            "\n**Fix:** Increase cache size to reduce contention:\n"
            "```sql\nALTER SEQUENCE seq_name CACHE 100;\n```"
        )
        bottlenecks.append((3, "Sequence Caching Issues", detail))

    # -- Temp file usage (PG) --
    if temp_sql_rows:
        detail = "Queries spilling to temp files:\n\n"
        detail += (
            "| queryid | Temp MB | Calls | Elapsed | Query |\n"
            "| --- | --- | --- | --- | --- |\n"
        )
        for r in temp_sql_rows[:10]:
            qid = str(r.get("queryid", "?"))
            tmb = _safe_float(r.get("temp_mb", 0))
            calls = _safe_int(r.get("calls", 0))
            elapsed = _safe_float(r.get("total_exec_sec", 0))
            sql = _truncate_sql(str(r.get("query_text", "")), 60)
            detail += (
                f"| `{qid}` | {tmb:.1f} | {calls:,} "
                f"| {_fmt_secs(elapsed)} | `{sql}` |\n"
            )
        detail += "\n**Fix:** Increase `work_mem` or optimise query to reduce sorting."
        bottlenecks.append((2, "Temp File Usage", detail))
        act_idx += 1
        actions.append(
            (
                2,
                f"{act_idx}. Increase `work_mem` -- "
                f"{len(temp_sql_rows)} queries spilling to disk",
            )
        )

    # -- Checkpoint issues (PG) --
    if ckpt_rows:
        ck = ckpt_rows[0]
        req = _safe_int(ck.get("checkpoints_req", 0))
        timed = _safe_int(ck.get("checkpoints_timed", 0))
        buffers_ckpt = _safe_int(ck.get("buffers_checkpoint", 0))
        buffers_be = _safe_int(ck.get("buffers_backend", 0))
        backend_pct = 0.0
        if buffers_ckpt + buffers_be > 0:
            backend_pct = buffers_be / (buffers_ckpt + buffers_be) * 100
        if req > timed and timed > 0:
            detail = (
                f"Requested checkpoints ({req:,}) **exceed** timed "
                f"checkpoints ({timed:,})\n\n"
                f"Backend write %: {backend_pct:.1f}%\n\n"
                "**Fix:** Increase `max_wal_size` and `checkpoint_timeout`."
            )
            bottlenecks.append((2, "Checkpoint Pressure", detail))
            act_idx += 1
            actions.append(
                (
                    1,
                    f"{act_idx}. Increase `max_wal_size` -- "
                    f"requested checkpoints ({req:,}) > timed ({timed:,})",
                )
            )
        if backend_pct > 20:
            detail_be = (
                f"**{backend_pct:.1f}%** of buffers written by backends "
                f"(should be < 5%)\n\n"
                "**Fix:** Increase `shared_buffers`, tune `bgwriter_*` params."
            )
            bottlenecks.append((2, "Backend Buffer Writes", detail_be))

    # -- Replication lag --
    if repl_rows:
        for r in repl_rows:
            replay_lag = _safe_float(r.get("replay_lag_sec", 0))
            client = r.get("client_addr", "?")
            state = r.get("state", "?")
            if replay_lag > 10:
                bottlenecks.append(
                    (
                        1 if replay_lag > 60 else 2,
                        f"Replication Lag ({client})",
                        f"Replica `{client}` ({state}): "
                        f"replay lag = **{_fmt_secs(replay_lag)}**",
                    )
                )
                risks.append(
                    (
                        "Medium-High",
                        "High",
                        f"Replication lag {_fmt_secs(replay_lag)} on {client}",
                    )
                )

    # -- Oracle SGA info --
    sga_rows = _get_rows(data, "sga_info")
    if sga_rows:
        detail = "SGA Memory Allocation:\n\n"
        detail += "| Component | Size |\n| --- | --- |\n"
        for r in sga_rows:
            name = r.get("name", "?")
            sz = _safe_float(r.get("size_mb", 0))
            sz_str = f"{sz / 1024:.1f} GB" if sz >= 1024 else f"{sz:.0f} MB"
            detail += f"| {name} | {sz_str} |\n"
        bottlenecks.append((3, "SGA Configuration", detail))

    # -- Oracle tablespace I/O --
    ts_io_rows = _get_rows(data, "tablespace_io")
    if ts_io_rows:
        detail = "Tablespace I/O:\n\n"
        detail += (
            "| Tablespace | Phys Reads | Phys Writes | "
            "Read Time | Write Time |\n"
            "| --- | --- | --- | --- | --- |\n"
        )
        for r in ts_io_rows[:10]:
            ts = r.get("tablespace_name", "?")
            pr = _safe_int(r.get("physical_reads", 0))
            pw = _safe_int(r.get("physical_writes", 0))
            rt = _safe_float(r.get("read_time_sec", 0))
            wt = _safe_float(r.get("write_time_sec", 0))
            detail += (
                f"| {ts} | {pr:,} | {pw:,} | {_fmt_secs(rt)} | {_fmt_secs(wt)} |\n"
            )
        bottlenecks.append((3, "Tablespace I/O", detail))

    # -- Oracle redo log switches --
    redo_rows = _get_rows(data, "redo_log_switches")
    if redo_rows:
        max_switches = max(_safe_int(r.get("switches", 0)) for r in redo_rows)
        if max_switches > 10:
            detail = "Redo log switches per hour:\n\n"
            detail += "| Hour | Switches |\n| --- | --- |\n"
            for r in redo_rows[:12]:
                detail += (
                    f"| {r.get('switch_hour', '?')} "
                    f"| {_safe_int(r.get('switches', 0))} |\n"
                )
            detail += (
                f"\nPeak: **{max_switches} switches/hour** -- "
                f"consider increasing redo log size."
            )
            bottlenecks.append((2, "High Redo Log Switches", detail))
            act_idx += 1
            actions.append(
                (
                    1,
                    f"{act_idx}. Increase redo log size -- "
                    f"peak {max_switches} switches/hour",
                )
            )

    # -- Oracle temp usage --
    temp_rows = _get_rows(data, "temp_usage")
    if temp_rows:
        for r in temp_rows:
            pct = _safe_float(r.get("pct_used", 0))
            if pct > 80:
                ts = r.get("tablespace_name", "?")
                used = _safe_float(r.get("used_mb", 0))
                free = _safe_float(r.get("free_mb", 0))
                bottlenecks.append(
                    (
                        2,
                        f"Temp Tablespace `{ts}` at {pct:.0f}%",
                        f"Used: {used:.0f} MB, Free: {free:.0f} MB",
                    )
                )
                risks.append(("Medium", "High", f"Temp space exhaustion on {ts}"))

    # =====================================================================
    # PHASE 3 -- Generate formatted report
    # =====================================================================
    parts: list[str] = []

    # --- Header ---
    parts.append(f"# Performance Analysis Report -- {db_type.upper()}")
    parts.append("*Programmatic analysis v2 -- no LLM involved*\n")
    parts.append("---\n")

    # --- 1. Executive Summary ---
    sev1 = [b for b in bottlenecks if b[0] == 1]
    sev2 = [b for b in bottlenecks if b[0] == 2]
    sev3 = [b for b in bottlenecks if b[0] == 3]

    if sev1:
        health = "CRITICAL -- immediate action required"
    elif sev2:
        health = "WARNING -- important issues found"
    elif sev3:
        health = "ADVISORY -- minor improvements possible"
    else:
        health = "HEALTHY -- no significant issues detected"

    parts.append("## 1. Executive Summary\n")
    parts.append(f"**Overall health:** {health}\n")

    headlines: list[str] = []
    if not is_oracle:
        headlines.append(f"Buffer cache hit ratio: **{cache_hit:.2f}%**")
        headlines.append(f"Active backends: **{backends}**")
        headlines.append(
            f"Transactions: **{commits:,}** commits, **{rollbacks:,}** rollbacks"
        )
        if wal_bytes > 0:
            headlines.append(f"WAL generated: **{_fmt_bytes(wal_bytes)}**")
        if temp_bytes > 0:
            headlines.append(
                f"Temp files: **{temp_files:,}** files, **{_fmt_bytes(temp_bytes)}**"
            )
    else:
        headlines.append(f"Buffer cache hit ratio: **{ora_cache_hit:.2f}%**")
        headlines.append(f"Hard parse ratio: **{ora_hard_parse_pct:.1f}%**")
        if ora_commit_count + ora_rb_count > 0:
            headlines.append(
                f"Transactions: **{ora_commit_count:,}** commits, "
                f"**{ora_rb_count:,}** rollbacks"
            )
    headlines.append(
        f"Issues found: **{len(sev1)}** critical, "
        f"**{len(sev2)}** important, **{len(sev3)}** advisory"
    )
    for h in headlines:
        parts.append(f"- {h}")
    parts.append("")

    # --- 2. Database & Workload Distribution ---
    parts.append("## 2. Database & Workload Overview\n")
    if not is_oracle:
        parts.append(
            "| Metric | Value |\n"
            "| --- | --- |\n"
            f"| Cache hit ratio | {cache_hit:.2f}% |\n"
            f"| Active backends | {backends} |\n"
            f"| Commits | {commits:,} |\n"
            f"| Rollbacks | {rollbacks:,} |\n"
            f"| Blocks hit | {blks_hit:,} |\n"
            f"| Blocks read (disk) | {blks_read:,} |\n"
            f"| Temp files | {temp_files:,} |\n"
            f"| Temp bytes | {_fmt_bytes(temp_bytes)} |"
        )
        if wal_bytes > 0:
            parts.append(
                f"| WAL generated | {_fmt_bytes(wal_bytes)} |\n"
                f"| WAL FPI count | {wal_fpi:,} |\n"
                f"| WAL sync time | {wal_sync_time_ms / 1000:.1f} sec |\n"
                f"| WAL write time | {wal_write_time_ms / 1000:.1f} sec |"
            )
    else:
        parts.append(
            "| Metric | Value |\n"
            "| --- | --- |\n"
            f"| Buffer cache hit | {ora_cache_hit:.2f}% |\n"
            f"| Hard parse ratio | {ora_hard_parse_pct:.1f}% |\n"
            f"| Disk sort ratio | {ora_disk_sort_pct:.1f}% |\n"
            f"| Commits | {ora_commit_count:,} |\n"
            f"| Rollbacks | {ora_rb_count:,} |"
        )
    parts.append("")

    # Connection distribution
    if conn_rows:
        parts.append("**Connection Distribution:**\n")
        parts.append("| State | Count | Wait Type |\n| --- | --- | --- |")
        for r in conn_rows:
            state = r.get("state", "unknown") or "null"
            count = _safe_int(r.get("count", 0))
            wtype = r.get("wait_event_type", "None")
            parts.append(f"| {state} | {count} | {wtype} |")
        if idle_count > 50:
            act_idx += 1
            actions.append(
                (
                    1,
                    f"{act_idx}. Use connection pooling (PgBouncer) -- "
                    f"{idle_count} idle connections",
                )
            )
        parts.append("")

    # --- 3. Top Bottlenecks ---
    parts.append("## 3. Top Bottlenecks\n")
    if not bottlenecks:
        parts.append(
            "No significant performance bottlenecks detected in the collected data.\n"
        )
    else:
        sev_label = {
            1: "SEV-1 (Critical)",
            2: "SEV-2 (Important)",
            3: "SEV-3 (Advisory)",
        }
        sev_emoji = {1: "SEV-1", 2: "SEV-2", 3: "SEV-3"}
        bn_idx = 0
        for sev in (1, 2, 3):
            group = [b for b in bottlenecks if b[0] == sev]
            if not group:
                continue
            for _, title, detail in group:
                bn_idx += 1
                parts.append(
                    f"### {sev_emoji.get(sev, '')} {bn_idx}. "
                    f"{title} ({sev_label[sev]})\n"
                )
                parts.append(detail)
                parts.append("")

    # --- 4. Configuration Review ---
    section_num = 4
    if config_rows:
        parts.append(f"## {section_num}. Configuration Review\n")
        if is_oracle:
            parts.append("| Parameter | Value | Description |\n| --- | --- | --- |")
            for r in config_rows:
                name = r.get("name", "?")
                val = r.get("value", "?")
                desc = _truncate_sql(str(r.get("description", "")), 80)
                parts.append(f"| `{name}` | `{val}` | {desc} |")
        else:
            parts.append("| Parameter | Value | Unit |\n| --- | --- | --- |")
            risky_params: dict[str, str] = {}
            for r in config_rows:
                name = r.get("name", "?")
                val = r.get("setting", "?")
                unit = r.get("unit", "") or ""
                parts.append(f"| `{name}` | `{val}` | {unit} |")
                if name == "statement_timeout" and str(val) == "0":
                    risky_params[name] = (
                        "No statement timeout -- risk of runaway queries"
                    )
                if name == "idle_in_transaction_session_timeout" and str(val) == "0":
                    risky_params[name] = "No idle-in-tx timeout -- risk of bloat"
                if name == "max_connections":
                    max_conn = _safe_int(val)
                    if max_conn > 500:
                        risky_params[name] = (
                            f"max_connections={max_conn} is high -- "
                            f"use connection pooling"
                        )
            if risky_params:
                parts.append("\n**Risks:**")
                for param, msg in risky_params.items():
                    parts.append(f"- `{param}`: {msg}")
                    risks.append(("Medium", "Medium", msg))
        parts.append("")
        section_num += 1

    # --- 5. Risk Register ---
    if risks:
        parts.append(f"## {section_num}. Risk Register\n")
        parts.append("| Risk | Likelihood | Impact |\n| --- | --- | --- |")
        seen_risks: set[str] = set()
        for likelihood, impact, desc in risks:
            if desc not in seen_risks:
                seen_risks.add(desc)
                parts.append(f"| {desc} | {likelihood} | {impact} |")
        parts.append("")
        section_num += 1

    # --- 6. Prioritised Action Plan ---
    parts.append(f"## {section_num}. Prioritised Action Plan\n")
    if not actions:
        parts.append(
            "No critical action items -- database appears healthy "
            "based on collected data."
        )
    else:
        p0 = [a for a in actions if a[0] == 0]
        p1 = [a for a in actions if a[0] == 1]
        p2 = [a for a in actions if a[0] == 2]
        if p0:
            parts.append("### Priority 0 -- Immediate (this sprint)\n")
            for _, text in p0:
                parts.append(text)
            parts.append("")
        if p1:
            parts.append("### Priority 1 -- Structural\n")
            for _, text in p1:
                parts.append(text)
            parts.append("")
        if p2:
            parts.append("### Priority 2 -- Performance Hygiene\n")
            for _, text in p2:
                parts.append(text)
            parts.append("")

    return "\n".join(parts)


def _get_rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """Safely extract a list of row dicts from collected data."""
    val = data.get(key, [])
    if isinstance(val, list):
        return val
    return []


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
        """Parse an uploaded report file and display it."""
        parsed = parse_uploaded_report(file_content, file_name)
        return self._run_uploaded_report_analysis(parsed)

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
        # Fully programmatic analysis — Python code identifies all issues.
        # No LLM involved: codellama hallucinates generic advice.
        findings_report = _build_findings_report(raw_data)
        report_text = self._format_report(raw_data)

        return {
            "raw_data": raw_data,
            "report_text": report_text,
            "analysis": findings_report,
        }

    def _run_uploaded_report_analysis(self, report_text: str) -> dict[str, Any]:
        # For uploaded reports we cannot do structured analysis.
        # Display the parsed text as-is — no LLM involved.
        return {
            "raw_data": {},
            "report_text": report_text,
            "analysis": (
                "## Uploaded Report\n\n"
                "The parsed report content is shown below. "
                "For detailed programmatic analysis, use **Live** mode "
                "which queries the database directly.\n\n"
                "---\n\n" + report_text[:8000]
            ),
        }

    # -- Oracle collection ---------------------------------------------------

    def _collect_oracle(self) -> dict[str, Any]:
        sections: dict[str, Any] = {}
        queries = {
            "top_cpu_sql": _ORA_TOP_CPU_SQL,
            "top_elapsed_sql": _ORA_TOP_SQL,
            "high_elapsed_per_exec": _ORA_HIGH_ELAPSED_PER_EXEC,
            "high_execution_count": _ORA_HIGH_EXEC_COUNT,
            "full_table_scans": _ORA_FULL_TABLE_SCANS,
            "existing_indexes": _ORA_EXISTING_INDEXES,
            "stale_statistics": _ORA_STALE_STATS,
            "row_contention": _ORA_ROW_CONTENTION,
            "sequence_no_cache": _ORA_SEQUENCE_NO_CACHE,
            "wait_events": _ORA_WAIT_EVENTS,
            "system_stats": _ORA_SYS_STATS,
            "sga_info": _ORA_SGA,
            "tablespace_io": _ORA_TABLESPACE_IO,
            "redo_log_switches": _ORA_REDO_LOG_SWITCHES,
            "temp_usage": _ORA_TEMP_USAGE,
            "parallel_queries": _ORA_PARALLEL_QUERIES,
            "config_params": _ORA_CONFIG_PARAMS,
            "idle_sessions": _ORA_IDLE_SESSIONS,
        }
        for name, sql in queries.items():
            result = self.db_client.execute_query(sql)
            if "error" in result:
                sections[name] = {"error": result["error"]}
            else:
                sections[name] = result.get("rows", [])

        # Collect execution plans for top 5 SQL IDs
        top_sql_ids = self._extract_oracle_sql_ids(sections)
        plans: list[dict[str, Any]] = []
        for sql_id in top_sql_ids[:5]:
            plan_sql = _ORA_SQL_PLAN_DETAIL.format(sql_id=sql_id)
            result = self.db_client.execute_query(plan_sql)
            if "error" not in result:
                rows = result.get("rows", [])
                if rows:
                    plans.append({"sql_id": sql_id, "steps": rows})
        if plans:
            sections["execution_plans"] = plans

        sections["db_type"] = DB_TYPE_ORACLE
        return sections

    def _extract_oracle_sql_ids(self, sections: dict[str, Any]) -> list[str]:
        """Extract unique sql_ids from top SQL sections, ordered by elapsed time."""
        seen: set[str] = set()
        ids: list[str] = []
        for key in ("top_cpu_sql", "top_elapsed_sql", "full_table_scans"):
            data = sections.get(key, [])
            if isinstance(data, list):
                for row in data:
                    sid = row.get("sql_id", "")
                    if sid and sid not in seen:
                        seen.add(sid)
                        ids.append(sid)
        return ids

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

    def _get_pg_major_version(self) -> int:
        """Return the PostgreSQL major version number (e.g. 14, 15, 16, 17)."""
        result = self.db_client.execute_query(
            "SELECT current_setting('server_version_num')::int AS ver"
        )
        if "error" in result:
            return 0
        rows = result.get("rows", [])
        if rows:
            # server_version_num is e.g. 170001 for 17.1, 160004 for 16.4
            return int(rows[0].get("ver", 0)) // 10000
        return 0

    def _collect_postgresql(self) -> dict[str, Any]:
        sections: dict[str, Any] = {}
        pg_major = self._get_pg_major_version()
        bgwriter_sql = _PG_BGWRITER_V17 if pg_major >= 17 else _PG_BGWRITER_LEGACY
        checkpoint_sql = (
            _PG_CHECKPOINT_STATS_V17 if pg_major >= 17 else _PG_CHECKPOINT_STATS_LEGACY
        )
        queries = {
            "top_cpu_queries": _PG_TOP_CPU_QUERIES,
            "top_queries": _PG_TOP_QUERIES,
            "high_elapsed_per_exec": _PG_HIGH_ELAPSED_PER_EXEC,
            "high_execution_count": _PG_HIGH_EXEC_COUNT,
            "seq_scan_tables": _PG_SEQ_SCAN_TABLES,
            "existing_indexes": _PG_EXISTING_INDEXES,
            "stale_stats_vacuum": _PG_STALE_STATS,
            "table_stats": _PG_TABLE_STATS,
            "table_sizes": _PG_TABLE_SIZES,
            "database_stats": _PG_DB_STATS,
            "bgwriter_stats": bgwriter_sql,
            "unused_indexes": _PG_UNUSED_INDEXES,
            "lock_waits": _PG_LOCK_WAITS,
            "bloat_estimate": _PG_BLOAT_ESTIMATE,
            "sequence_cache_issues": _PG_SEQUENCE_CACHE,
            "temp_file_usage": _PG_TEMP_FILE_USAGE,
            "connection_stats": _PG_CONNECTION_STATS,
            "checkpoint_stats": checkpoint_sql,
            "idle_in_transaction": _PG_IDLE_IN_TRANSACTION,
            "config_params": _PG_CONFIG_PARAMS,
            "replication_status": _PG_REPLICATION_STATUS,
        }
        # WAL stats only available in PG 14+
        if pg_major >= 14:
            queries["wal_stats"] = _PG_WAL_STATS
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
        parts = [
            f"REAL DATABASE PERFORMANCE DATA ({db_type.upper()})\n{'=' * 60}\n",
            "Below is REAL data collected from a live database. "
            "Analyse ONLY this data. Do NOT invent sql_ids, table names, or queries "
            "that do not appear below.\n",
        ]

        for section_name, section_data in data.items():
            if section_name in ("db_type", "snap_range", "sample_range"):
                continue
            parts.append(f"\n--- {section_name.upper().replace('_', ' ')} ---")
            if isinstance(section_data, dict) and "error" in section_data:
                parts.append(f"  ERROR: {section_data['error']}")
            elif section_name == "execution_plans" and isinstance(section_data, list):
                for plan in section_data:
                    parts.append(f"\n  PLAN FOR sql_id={plan.get('sql_id', '?')}:")
                    for step in plan.get("steps", [])[:20]:
                        parts.append(f"    {_format_row(step)}")
            elif isinstance(section_data, list):
                if not section_data:
                    parts.append("  (no data)")
                else:
                    limit = (
                        25
                        if section_name
                        in (
                            "existing_indexes",
                            "stale_statistics",
                            "stale_stats_vacuum",
                        )
                        else 15
                    )
                    for i, row in enumerate(section_data[:limit]):
                        parts.append(f"  [{i + 1}] {_format_row(row)}")
                    if len(section_data) > limit:
                        parts.append(f"  ... and {len(section_data) - limit} more rows")
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
