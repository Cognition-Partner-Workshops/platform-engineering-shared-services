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


def _build_findings_report(data: dict[str, Any]) -> str:
    """Analyse collected data programmatically and build a markdown report.

    This function does the actual analysis in Python code — identifying
    problematic SQL, full table scans, missing indexes, etc. from the
    real data. No LLM is involved in finding issues.
    """
    db_type = data.get("db_type", "unknown")
    is_oracle = db_type == DB_TYPE_ORACLE
    parts: list[str] = []
    action_items: list[str] = []
    action_idx = 0

    parts.append(f"# Performance Analysis Report ({db_type.upper()})")
    parts.append("")

    # =====================================================================
    # DATABASE-LEVEL OVERVIEW
    # =====================================================================

    # --- Database Stats (PostgreSQL) ------------------------------------------
    if not is_oracle:
        db_rows = _get_rows(data, "database_stats")
        if db_rows:
            row = db_rows[0]
            cache_hit = _safe_float(row.get("cache_hit_pct", 0))
            commits = _safe_int(row.get("xact_commit", 0))
            rollbacks = _safe_int(row.get("xact_rollback", 0))
            backends = _safe_int(row.get("numbackends", 0))
            blks_read = _safe_int(row.get("blks_read", 0))
            blks_hit = _safe_int(row.get("blks_hit", 0))
            temp_bytes = _safe_int(row.get("temp_bytes", 0))
            temp_files = _safe_int(row.get("temp_files", 0))
            parts.append("## Database Overview")
            parts.append(
                f"- **Cache hit ratio:** {cache_hit:.2f}%\n"
                f"- **Active backends:** {backends}\n"
                f"- **Transactions:** {commits:,} commits, {rollbacks:,} rollbacks\n"
                f"- **Blocks:** {blks_hit:,} hit, {blks_read:,} read from disk\n"
                f"- **Temp usage:** {temp_files:,} files, "
                f"{temp_bytes / 1048576:.1f} MB"
            )
            if cache_hit < 95 and blks_read > 0:
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[CACHE]** Cache hit ratio is {cache_hit:.2f}% "
                    f"(target > 99%). Increase `shared_buffers`."
                )
            if rollbacks > 0 and commits > 0:
                rb_pct = rollbacks / (commits + rollbacks) * 100
                if rb_pct > 5:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[ROLLBACKS]** {rb_pct:.1f}% rollback rate "
                        f"({rollbacks:,}/{commits + rollbacks:,}). "
                        f"Investigate application error handling."
                    )
            parts.append("")

    # --- Connection Stats (PostgreSQL) ----------------------------------------
    if not is_oracle:
        conn_rows = _get_rows(data, "connection_stats")
        if conn_rows:
            parts.append("## Connection Stats")
            for row in conn_rows:
                state = row.get("state", "unknown") or "null"
                count = _safe_int(row.get("count", 0))
                wtype = row.get("wait_event_type", "None")
                parts.append(f"- **{state}**: {count} connections (wait: {wtype})")
            idle_count = sum(
                _safe_int(r.get("count", 0))
                for r in conn_rows
                if (r.get("state") or "").startswith("idle")
            )
            if idle_count > 50:
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[CONNECTIONS]** {idle_count} idle connections. "
                    f"Use connection pooling (PgBouncer)."
                )
            parts.append("")

    # --- Oracle System Stats --------------------------------------------------
    if is_oracle:
        sys_rows = _get_rows(data, "system_stats") or _get_rows(
            data, "awr_system_stats"
        )
        if sys_rows:
            parts.append("## System Statistics")
            stats_map: dict[str, int] = {}
            for row in sys_rows:
                name = str(row.get("name", ""))
                val = _safe_int(row.get("value", 0))
                stats_map[name] = val
                parts.append(f"- **{name}:** {val:,}")
            # Cache hit ratio
            db_gets = stats_map.get("db block gets", 0)
            consistent = stats_map.get("consistent gets", 0)
            phys_reads = stats_map.get("physical reads", 0)
            logical = db_gets + consistent
            if logical > 0:
                hit_pct = (1 - phys_reads / logical) * 100
                parts.append(f"\n**Buffer cache hit ratio: {hit_pct:.2f}%**")
                if hit_pct < 95:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[CACHE]** Buffer cache hit ratio is "
                        f"{hit_pct:.2f}% (target > 99%). "
                        f"Increase `db_cache_size`."
                    )
            hard_parse = stats_map.get("parse count (hard)", 0)
            total_parse = stats_map.get("parse count (total)", 0)
            if total_parse > 0:
                hard_pct = hard_parse / total_parse * 100
                if hard_pct > 30:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[PARSING]** Hard parse ratio is "
                        f"{hard_pct:.1f}%. Use bind variables."
                    )
            sorts_disk = stats_map.get("sorts (disk)", 0)
            sorts_mem = stats_map.get("sorts (memory)", 0)
            if sorts_disk > 0 and sorts_mem > 0:
                disk_pct = sorts_disk / (sorts_mem + sorts_disk) * 100
                if disk_pct > 5:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[SORTS]** {disk_pct:.1f}% sorts on disk "
                        f"({sorts_disk:,}/{sorts_mem + sorts_disk:,}). "
                        f"Increase `sort_area_size` / `PGA_AGGREGATE_TARGET`."
                    )
            parts.append("")

    # --- Oracle SGA Info ------------------------------------------------------
    if is_oracle:
        sga_rows = _get_rows(data, "sga_info")
        if sga_rows:
            parts.append("## SGA Configuration")
            for row in sga_rows:
                name = row.get("name", "?")
                size_mb = _safe_float(row.get("size_mb", 0))
                parts.append(f"- **{name}:** {size_mb:.0f} MB")
            parts.append("")

    # --- Oracle Tablespace I/O ------------------------------------------------
    if is_oracle:
        ts_rows = _get_rows(data, "tablespace_io")
        if ts_rows:
            parts.append("## Tablespace I/O")
            for row in ts_rows:
                ts_name = row.get("tablespace_name", "?")
                reads = _safe_int(row.get("physical_reads", 0))
                writes = _safe_int(row.get("physical_writes", 0))
                read_sec = _safe_float(row.get("read_time_sec", 0))
                write_sec = _safe_float(row.get("write_time_sec", 0))
                parts.append(
                    f"- **`{ts_name}`** — reads: {reads:,} ({read_sec:.2f}s), "
                    f"writes: {writes:,} ({write_sec:.2f}s)"
                )
                if read_sec > 10:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[I/O]** Tablespace `{ts_name}` has "
                        f"{read_sec:.2f}s read time. Move to faster storage or "
                        f"redistribute I/O."
                    )
            parts.append("")

    # --- Oracle Redo Log Switches ---------------------------------------------
    if is_oracle:
        redo_rows = _get_rows(data, "redo_log_switches")
        if redo_rows:
            parts.append("## Redo Log Switches (Last 24h)")
            for row in redo_rows:
                hour = row.get("switch_hour", "?")
                switches = _safe_int(row.get("switches", 0))
                parts.append(f"- **{hour}:** {switches} switches")
                if switches > 10:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[REDO]** {switches} log switches in hour "
                        f"{hour}. Increase redo log file size."
                    )
            parts.append("")

    # --- Oracle Temp Usage ----------------------------------------------------
    if is_oracle:
        temp_rows = _get_rows(data, "temp_usage")
        if temp_rows:
            parts.append("## Temp Tablespace Usage")
            for row in temp_rows:
                ts_name = row.get("tablespace_name", "?")
                used_mb = _safe_float(row.get("used_mb", 0))
                free_mb = _safe_float(row.get("free_mb", 0))
                pct = _safe_float(row.get("pct_used", 0))
                parts.append(
                    f"- **`{ts_name}`** — {used_mb:.0f} MB used, "
                    f"{free_mb:.0f} MB free ({pct:.1f}% used)"
                )
                if pct > 80:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[TEMP]** `{ts_name}` is {pct:.1f}% full. "
                        f"Add temp datafile or resize."
                    )
            parts.append("")

    # =====================================================================
    # TOP SQL BY RESOURCE CONSUMPTION
    # =====================================================================

    # --- Top CPU SQL (always show — this is the most important section) -------
    cpu_key = "top_cpu_sql" if is_oracle else "top_cpu_queries"
    cpu_rows = _get_rows(data, cpu_key)
    # Also check AWR top SQL / pgProfile top SQL as alternatives
    if not cpu_rows:
        cpu_rows = _get_rows(data, "awr_top_sql")
    if not cpu_rows:
        cpu_rows = _get_rows(data, "pgprofile_top_sql")
    if cpu_rows:
        parts.append("## Top SQL by CPU / Elapsed Time")
        parts.append("")
        for i, row in enumerate(cpu_rows[:15]):
            sid = row.get("sql_id") or row.get("queryid") or "?"
            id_label = "sql_id" if is_oracle else "queryid"
            if is_oracle:
                cpu_sec = _safe_float(row.get("cpu_sec", 0))
                elapsed_sec = _safe_float(row.get("elapsed_sec", 0))
                execs = _safe_int(row.get("executions", 0))
                gets = _safe_int(row.get("buffer_gets", 0))
                gets_per = _safe_int(row.get("gets_per_exec", 0))
                sql_text = str(row.get("sql_text") or "")
                parts.append(
                    f"**{i + 1}. {id_label}: `{sid}`** — "
                    f"CPU: {cpu_sec:.2f}s, elapsed: {elapsed_sec:.2f}s, "
                    f"{execs:,} executions, buffer gets: {gets:,} "
                    f"({gets_per:,}/exec)"
                )
            else:
                total_sec = _safe_float(
                    row.get("total_exec_sec") or row.get("total_exec_time", 0)
                )
                mean_sec = _safe_float(
                    row.get("mean_exec_sec") or row.get("mean_exec_time", 0)
                )
                calls = _safe_int(row.get("calls", 0))
                cache_hit = _safe_float(row.get("cache_hit_pct", 100))
                blk_read = _safe_int(row.get("shared_blks_read", 0))
                blk_hit = _safe_int(row.get("shared_blks_hit", 0))
                parts.append(
                    f"**{i + 1}. {id_label}: `{sid}`** — "
                    f"total: {total_sec:.2f}s, avg: {mean_sec:.4f}s/call, "
                    f"{calls:,} calls, cache hit: {cache_hit:.1f}%, "
                    f"blks read: {blk_read:,}, blks hit: {blk_hit:,}"
                )
            sql_text = str(row.get("sql_text") or row.get("query_text") or "")
            if sql_text:
                parts.append(f"```sql\n{_truncate_sql(sql_text, 400)}\n```")
            # Generate action items for top offenders
            if i < 5:
                if is_oracle:
                    if gets_per > 10000:
                        action_idx += 1
                        action_items.append(
                            f"{action_idx}. **[TOP CPU]** `{sid}` — "
                            f"{gets_per:,} buffer gets/exec. "
                            f"Review execution plan: "
                            f"`SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR"
                            f"('{sid}'));`"
                        )
                else:
                    if cache_hit < 90 and blk_read > 1000:
                        action_idx += 1
                        action_items.append(
                            f"{action_idx}. **[LOW CACHE HIT]** queryid `{sid}` — "
                            f"cache hit {cache_hit:.1f}%, {blk_read:,} blocks read. "
                            f"Add indexes or increase `shared_buffers`."
                        )
                    if mean_sec > 1.0:
                        action_idx += 1
                        action_items.append(
                            f"{action_idx}. **[SLOW QUERY]** queryid `{sid}` — "
                            f"avg {mean_sec:.4f}s/call. Run "
                            f"`EXPLAIN (ANALYZE, BUFFERS) <query>` to investigate."
                        )
        parts.append("")

    # --- Top Queries by Total Elapsed (fallback if different from CPU) --------
    elapsed_key = "top_elapsed_sql" if is_oracle else "top_queries"
    elapsed_rows = _get_rows(data, elapsed_key)
    # Only show if we have data AND it's different from cpu_rows
    if elapsed_rows and elapsed_key != cpu_key:
        # Check if these are substantially different from the CPU rows
        cpu_ids = (
            {str(r.get("sql_id") or r.get("queryid") or "") for r in cpu_rows[:10]}
            if cpu_rows
            else set()
        )
        new_rows = [
            r
            for r in elapsed_rows
            if str(r.get("sql_id") or r.get("queryid") or "") not in cpu_ids
        ]
        if new_rows:
            parts.append("## Additional Top SQL by Total Elapsed Time")
            parts.append("*(Not already listed in Top CPU section)*\n")
            for i, row in enumerate(new_rows[:10]):
                sid = row.get("sql_id") or row.get("queryid") or "?"
                id_label = "sql_id" if is_oracle else "queryid"
                if is_oracle:
                    elapsed_sec = _safe_float(row.get("elapsed_sec", 0))
                    execs = _safe_int(row.get("executions", 0))
                    gets = _safe_int(row.get("buffer_gets", 0))
                    sql_text = str(row.get("sql_text") or "")
                    parts.append(
                        f"**{i + 1}. {id_label}: `{sid}`** — "
                        f"elapsed: {elapsed_sec:.2f}s, {execs:,} execs, "
                        f"buffer gets: {gets:,}"
                    )
                else:
                    total_sec = _safe_float(row.get("total_exec_sec", 0))
                    mean_sec = _safe_float(row.get("mean_exec_sec", 0))
                    calls = _safe_int(row.get("calls", 0))
                    cache_hit = _safe_float(row.get("cache_hit_pct", 100))
                    parts.append(
                        f"**{i + 1}. {id_label}: `{sid}`** — "
                        f"total: {total_sec:.2f}s, avg: {mean_sec:.4f}s/call, "
                        f"{calls:,} calls, cache hit: {cache_hit:.1f}%"
                    )
                sql_text = str(row.get("sql_text") or row.get("query_text") or "")
                if sql_text:
                    parts.append(f"```sql\n{_truncate_sql(sql_text, 400)}\n```")
            parts.append("")

    # =====================================================================
    # THRESHOLD-BASED FINDINGS
    # =====================================================================

    # --- High Elapsed Time SQL ------------------------------------------------
    section_key = "high_elapsed_per_exec"
    rows = _get_rows(data, section_key)
    parts.append("## High Elapsed Time per Execution (> 1s avg)")
    if not rows:
        parts.append("No queries exceed the 1s/exec threshold.\n")
    else:
        parts.append("")
        for row in rows:
            sid = row.get("sql_id") or row.get("queryid") or "?"
            avg_elapsed = _safe_float(row.get("avg_elapsed_sec", 0))
            total_elapsed = _safe_float(
                row.get("total_elapsed_sec") or row.get("total_exec_sec", 0)
            )
            execs = _safe_int(row.get("executions") or row.get("calls", 0))
            sql_text = str(row.get("sql_text") or row.get("query_text") or "")
            gets = _safe_int(row.get("buffer_gets") or row.get("shared_blks_read", 0))
            id_label = "sql_id" if is_oracle else "queryid"
            parts.append(
                f"**{id_label}: `{sid}`** — "
                f"avg {avg_elapsed:.4f}s/exec, {execs:,} executions, "
                f"total {total_elapsed:.2f}s, buffer gets/reads: {gets:,}"
            )
            if sql_text:
                parts.append(f"```sql\n{_truncate_sql(sql_text, 400)}\n```")
            action_idx += 1
            if is_oracle:
                action_items.append(
                    f"{action_idx}. **[HIGH ELAPSED]** `{sid}` "
                    f"(avg {avg_elapsed:.4f}s/exec). Check plan: "
                    f"`SELECT * FROM TABLE(DBMS_XPLAN.DISPLAY_CURSOR"
                    f"('{sid}'));` — add indexes on WHERE/JOIN columns."
                )
            else:
                action_items.append(
                    f"{action_idx}. **[HIGH ELAPSED]** queryid `{sid}` "
                    f"(avg {avg_elapsed:.4f}s/exec). Run "
                    f"`EXPLAIN (ANALYZE, BUFFERS)` on this query and "
                    f"add indexes on filtered columns."
                )
        parts.append("")

    # --- High Execution Count SQL ---------------------------------------------
    section_key = "high_execution_count"
    rows = _get_rows(data, section_key)
    parts.append("## High Execution Count SQL (> 1000 calls)")
    if not rows:
        parts.append("No queries exceed the 1000 execution threshold.\n")
    else:
        parts.append("")
        for row in rows:
            sid = row.get("sql_id") or row.get("queryid") or "?"
            execs = _safe_int(row.get("executions") or row.get("calls", 0))
            total_elapsed = _safe_float(
                row.get("total_elapsed_sec") or row.get("total_exec_sec", 0)
            )
            sql_text = str(row.get("sql_text") or row.get("query_text") or "")
            id_label = "sql_id" if is_oracle else "queryid"
            parts.append(
                f"**{id_label}: `{sid}`** — "
                f"{execs:,} executions, total {total_elapsed:.2f}s"
            )
            if sql_text:
                parts.append(f"```sql\n{_truncate_sql(sql_text, 400)}\n```")
            if execs > 100000:
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[HIGH EXEC COUNT]** `{sid}` executed "
                    f"{execs:,} times. Consider caching results, batching, "
                    f"or reducing call frequency."
                )
        parts.append("")

    # --- Full Table Scans -----------------------------------------------------
    fts_key = "full_table_scans" if is_oracle else "seq_scan_tables"
    rows = _get_rows(data, fts_key)
    parts.append("## Full Table Scans / Sequential Scans")
    if not rows:
        parts.append("No issues found.\n")
    else:
        parts.append("")
        for row in rows:
            if is_oracle:
                table = row.get("table_name", "?")
                owner = row.get("object_owner", "")
                sid = row.get("sql_id", "?")
                execs = _safe_int(row.get("executions", 0))
                elapsed = _safe_float(row.get("elapsed_sec", 0))
                gets = _safe_int(row.get("buffer_gets", 0))
                sql_text = str(row.get("sql_text") or "")
                parts.append(
                    f"**Table: `{owner}.{table}`** — sql_id: `{sid}`, "
                    f"{execs:,} execs, {elapsed:.2f}s elapsed, "
                    f"{gets:,} buffer gets"
                )
                if sql_text:
                    parts.append(f"```sql\n{_truncate_sql(sql_text, 400)}\n```")
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[FULL TABLE SCAN]** `{owner}.{table}` "
                    f"via sql_id `{sid}`. Add index on columns in WHERE clause "
                    f"or use hints to force index access."
                )
            else:
                table = row.get("relname", "?")
                schema = row.get("schemaname", "public")
                seq_scans = _safe_int(row.get("seq_scan", 0))
                seq_tup_read = _safe_int(row.get("seq_tup_read", 0))
                idx_scans = _safe_int(row.get("idx_scan", 0))
                live_tup = _safe_int(row.get("n_live_tup", 0))
                size_mb = _safe_float(row.get("table_size_mb", 0))
                avg_rows = _safe_int(row.get("avg_rows_per_seq_scan", 0))
                parts.append(
                    f"**Table: `{schema}.{table}`** — "
                    f"{seq_scans:,} seq scans ({avg_rows:,} rows/scan avg, "
                    f"{seq_tup_read:,} rows read), "
                    f"{idx_scans:,} idx scans, {live_tup:,} live rows, "
                    f"{size_mb:.1f} MB"
                )
                if seq_scans > 100 and live_tup > 10000:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[SEQ SCAN]** `{schema}.{table}` has "
                        f"{seq_scans:,} seq scans on {live_tup:,} rows "
                        f"({size_mb:.1f} MB). Add indexes on frequently "
                        f"filtered columns: "
                        f"`CREATE INDEX ON {schema}.{table} (column_name);`"
                    )
        parts.append("")

    # --- Execution Plans (Oracle) ---------------------------------------------
    if is_oracle:
        plans = data.get("execution_plans", [])
        if isinstance(plans, list) and plans:
            parts.append("## Execution Plans (Top SQL)")
            parts.append("")
            for plan in plans[:5]:
                sid = plan.get("sql_id", "?")
                steps = plan.get("steps", [])
                parts.append(f"### Plan for sql_id: `{sid}`")
                has_full_scan = False
                has_hash_join = False
                for step in steps[:20]:
                    op = str(step.get("operation", ""))
                    obj = step.get("object_name", "")
                    cost = step.get("cost", "")
                    est = step.get("est_rows", "")
                    line = f"- {op}"
                    if obj:
                        line += f" on `{obj}`"
                    if cost:
                        line += f" (cost={cost}, rows={est})"
                    parts.append(line)
                    if "FULL" in op.upper():
                        has_full_scan = True
                    if "HASH JOIN" in op.upper():
                        has_hash_join = True
                if has_full_scan:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[PLAN]** sql_id `{sid}` has TABLE ACCESS "
                        f"FULL in plan. Add appropriate index."
                    )
                if has_hash_join:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[PLAN]** sql_id `{sid}` uses HASH JOIN. "
                        f"Ensure join columns are indexed for NESTED LOOPS "
                        f"if table is small."
                    )
                parts.append("")

    # --- Oracle Parallel Queries ----------------------------------------------
    if is_oracle:
        px_rows = _get_rows(data, "parallel_queries")
        if px_rows:
            parts.append("## Parallel Queries")
            for row in px_rows:
                sid = row.get("sql_id", "?")
                px = _safe_int(row.get("px_servers", 0))
                elapsed = _safe_float(row.get("elapsed_sec", 0))
                sql_text = str(row.get("sql_text") or "")
                parts.append(
                    f"- **sql_id: `{sid}`** — {px:,} PX servers, {elapsed:.2f}s elapsed"
                )
                if sql_text:
                    parts.append(f"```sql\n{_truncate_sql(sql_text, 300)}\n```")
            parts.append("")

    # --- Row Contention & Locking ---------------------------------------------
    contention_key = "row_contention" if is_oracle else "lock_waits"
    rows = _get_rows(data, contention_key)
    parts.append("## Row Contention & Locking")
    if not rows:
        parts.append("No active contention detected.\n")
    else:
        parts.append("")
        for row in rows:
            if is_oracle:
                event = row.get("event", "?")
                waits = _safe_int(row.get("total_waits", 0))
                waited_sec = _safe_float(row.get("time_waited_sec", 0))
                avg_wait = _safe_float(row.get("avg_wait_sec", 0))
                parts.append(
                    f"**Event: `{event}`** — {waits:,} waits, "
                    f"{waited_sec:.2f}s total, avg {avg_wait:.4f}s/wait"
                )
                if waited_sec > 1:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[CONTENTION]** `{event}` — "
                        f"{waited_sec:.2f}s total. Reduce hot-row updates, "
                        f"increase INITRANS, or tune locking strategy."
                    )
            else:
                pid = row.get("pid", "?")
                user = row.get("usename", "?")
                event = row.get("wait_event", "?")
                event_type = row.get("wait_event_type", "")
                running_sec = _safe_float(row.get("running_sec", 0))
                state = row.get("state", "")
                query = str(row.get("query") or "")
                parts.append(
                    f"**PID {pid}** (user: {user}, state: {state}) — "
                    f"wait: {event_type}/{event}, running {running_sec:.2f}s"
                )
                if query:
                    parts.append(f"```sql\n{_truncate_sql(query, 300)}\n```")
                if running_sec > 60:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[LONG WAIT]** PID {pid} waiting on "
                        f"{event_type}/{event} for {running_sec:.0f}s. "
                        f"Consider `SELECT pg_cancel_backend({pid});`"
                    )
        parts.append("")

    # --- Wait Events (Oracle / AWR) -------------------------------------------
    if is_oracle:
        wait_rows = _get_rows(data, "wait_events") or _get_rows(data, "awr_wait_events")
        if wait_rows:
            parts.append("## Top Wait Events")
            parts.append("")
            for row in wait_rows[:15]:
                event = row.get("event", "?")
                waits = _safe_int(row.get("total_waits", 0))
                waited = _safe_float(row.get("time_waited_sec", 0))
                avg_w = _safe_float(row.get("avg_wait_sec", 0))
                line = f"- **`{event}`** — {waits:,} waits, {waited:.2f}s total"
                if avg_w > 0:
                    line += f", avg {avg_w:.4f}s"
                parts.append(line)
                if waited > 60:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[WAIT]** `{event}` — "
                        f"{waited:.2f}s total wait time. "
                        f"Investigate root cause (I/O, lock, latch)."
                    )
            parts.append("")

    # --- pgProfile Wait Events ------------------------------------------------
    if not is_oracle:
        pgp_wait_rows = _get_rows(data, "pgprofile_wait_events")
        if pgp_wait_rows:
            parts.append("## Wait Events (pgProfile)")
            for row in pgp_wait_rows[:15]:
                etype = row.get("event_type", "?")
                event = row.get("event", "?")
                waits = _safe_int(row.get("total_waits", 0))
                waited = _safe_float(row.get("total_waited_sec", 0))
                parts.append(f"- **{etype}/{event}** — {waits:,} waits, {waited:.2f}s")
            parts.append("")

    # --- Sequence Caching Issues -----------------------------------------------
    seq_key = "sequence_no_cache" if is_oracle else "sequence_cache_issues"
    rows = _get_rows(data, seq_key)
    parts.append("## Sequence Caching Issues")
    if not rows:
        parts.append("No issues found.\n")
    else:
        parts.append("")
        for row in rows:
            if is_oracle:
                owner = row.get("sequence_owner", "")
                name = row.get("sequence_name", "?")
                cache = _safe_int(row.get("cache_size", 0))
                parts.append(
                    f"**`{owner}.{name}`** — cache_size={cache} (should be >= 20)"
                )
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[SEQUENCE]** "
                    f"`ALTER SEQUENCE {owner}.{name} CACHE 20;`"
                )
            else:
                schema = row.get("schemaname", "public")
                name = row.get("sequencename", "?")
                cache = _safe_int(row.get("cache_size") or 0)
                parts.append(
                    f"**`{schema}.{name}`** — cache_size={cache} (should be >= 20)"
                )
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[SEQUENCE]** "
                    f"`ALTER SEQUENCE {schema}.{name} CACHE 20;`"
                )
        parts.append("")

    # --- Stale Statistics / Vacuum / Bloat ------------------------------------
    if is_oracle:
        rows = _get_rows(data, "stale_statistics")
    else:
        rows = _get_rows(data, "stale_stats_vacuum") + _get_rows(data, "bloat_estimate")
        seen_tables: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in rows:
            key = f"{r.get('schemaname', '')}.{r.get('relname', '')}"
            if key not in seen_tables:
                seen_tables.add(key)
                deduped.append(r)
        rows = deduped

    parts.append("## Stale Statistics / Vacuum / Bloat")
    if not rows:
        parts.append("No issues found.\n")
    else:
        parts.append("")
        for row in rows:
            if is_oracle:
                table = row.get("table_name", "?")
                num_rows = _safe_int(row.get("num_rows", 0))
                stale = row.get("stale_stats", "?")
                last_analyzed = row.get("last_analyzed", "never")
                days = _safe_float(row.get("days_since_analyzed", 0))
                parts.append(
                    f"**`{table}`** — {num_rows:,} rows, stale={stale}, "
                    f"last analyzed: {last_analyzed} ({days:.0f} days ago)"
                )
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[STALE STATS]** "
                    f"`EXEC DBMS_STATS.GATHER_TABLE_STATS"
                    f"(ownname=>USER, tabname=>'{table}');`"
                )
            else:
                schema = row.get("schemaname", "public")
                table = row.get("relname", "?")
                dead = _safe_int(row.get("n_dead_tup", 0))
                live = _safe_int(row.get("n_live_tup", 0))
                dead_pct = _safe_float(row.get("dead_pct", 0))
                last_vac = (
                    row.get("last_autovacuum") or row.get("last_vacuum") or "never"
                )
                last_analyze = (
                    row.get("last_autoanalyze") or row.get("last_analyze") or "never"
                )
                parts.append(
                    f"**`{schema}.{table}`** — {live:,} live, {dead:,} dead "
                    f"({dead_pct:.1f}% bloat), last vacuum: {last_vac}, "
                    f"last analyze: {last_analyze}"
                )
                if dead_pct > 20 or dead > 50000:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[BLOAT]** "
                        f"`VACUUM ANALYZE {schema}.{table};` "
                        f"— {dead_pct:.1f}% dead tuples"
                    )
                elif str(last_analyze) in ("never", "None"):
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[STALE STATS]** "
                        f"`ANALYZE {schema}.{table};` — never analyzed"
                    )
        parts.append("")

    # --- Unused Indexes -------------------------------------------------------
    rows = _get_rows(data, "unused_indexes")
    if rows:
        parts.append("## Unused Indexes")
        parts.append("")
        for row in rows:
            schema = row.get("schemaname", "public")
            table = row.get("relname", "?")
            idx_name = row.get("indexrelname", "?")
            size_mb = _safe_float(row.get("index_size_mb", 0))
            parts.append(
                f"**`{schema}.{idx_name}`** on `{table}` — {size_mb:.1f} MB, 0 scans"
            )
            if size_mb > 1:
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[UNUSED INDEX]** "
                    f"`DROP INDEX {schema}.{idx_name};` — "
                    f"{size_mb:.1f} MB wasted"
                )
        parts.append("")

    # --- Table Stats (PostgreSQL) — top tables by activity --------------------
    if not is_oracle:
        tbl_rows = _get_rows(data, "table_stats")
        if tbl_rows:
            parts.append("## Top Tables by Activity")
            parts.append("")
            for row in tbl_rows[:10]:
                schema = row.get("schemaname", "public")
                table = row.get("relname", "?")
                seq_scan = _safe_int(row.get("seq_scan", 0))
                idx_scan = _safe_int(row.get("idx_scan", 0))
                inserts = _safe_int(row.get("n_tup_ins", 0))
                updates = _safe_int(row.get("n_tup_upd", 0))
                deletes = _safe_int(row.get("n_tup_del", 0))
                live = _safe_int(row.get("n_live_tup", 0))
                dead = _safe_int(row.get("n_dead_tup", 0))
                parts.append(
                    f"- **`{schema}.{table}`** — seq: {seq_scan:,}, "
                    f"idx: {idx_scan:,}, ins/upd/del: "
                    f"{inserts:,}/{updates:,}/{deletes:,}, "
                    f"live: {live:,}, dead: {dead:,}"
                )
            parts.append("")

    # --- Checkpoint / WAL Issues (PostgreSQL) ---------------------------------
    if not is_oracle:
        cp_rows = _get_rows(data, "checkpoint_stats")
        parts.append("## Checkpoint / WAL Issues")
        has_issue = False
        if cp_rows:
            row = cp_rows[0]
            backend_pct = _safe_float(row.get("backend_write_pct", 0))
            req = _safe_int(row.get("checkpoints_req", 0))
            timed = _safe_int(row.get("checkpoints_timed", 0))
            buf_cp = _safe_int(row.get("buffers_checkpoint", 0))
            buf_clean = _safe_int(row.get("buffers_clean", 0))
            buf_backend = _safe_int(row.get("buffers_backend", 0))
            parts.append(
                f"- Checkpoints: {timed:,} timed, {req:,} requested\n"
                f"- Buffers: checkpoint={buf_cp:,}, clean={buf_clean:,}, "
                f"backend={buf_backend:,}\n"
                f"- Backend write %: {backend_pct:.1f}%"
            )
            if backend_pct > 10:
                has_issue = True
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[CHECKPOINT]** Backend writes are "
                    f"{backend_pct:.1f}% of total — increase "
                    f"`shared_buffers` and `checkpoint_completion_target`."
                )
            if req > timed and timed > 0:
                has_issue = True
                action_idx += 1
                action_items.append(
                    f"{action_idx}. **[CHECKPOINT]** More requested ({req:,}) "
                    f"than timed ({timed:,}) checkpoints — increase "
                    f"`max_wal_size`."
                )
        if not has_issue:
            parts.append("No issues found.")
        parts.append("")

    # --- Temp File Usage (PostgreSQL) -----------------------------------------
    if not is_oracle:
        rows = _get_rows(data, "temp_file_usage")
        if rows:
            parts.append("## Temp File Usage")
            parts.append("")
            for row in rows[:10]:
                sid = row.get("queryid", "?")
                temp_mb = _safe_float(row.get("temp_mb", 0))
                calls = _safe_int(row.get("calls", 0))
                sql_text = str(row.get("query_text") or "")
                parts.append(
                    f"**queryid: `{sid}`** — {temp_mb:.1f} MB temp, {calls:,} calls"
                )
                if sql_text:
                    parts.append(f"```sql\n{_truncate_sql(sql_text, 300)}\n```")
                if temp_mb > 100:
                    action_idx += 1
                    action_items.append(
                        f"{action_idx}. **[TEMP FILES]** queryid `{sid}` uses "
                        f"{temp_mb:.1f} MB temp. Increase `work_mem` or "
                        f"optimize sort/join."
                    )
            parts.append("")

    # =====================================================================
    # EXECUTIVE SUMMARY & ACTION PLAN
    # =====================================================================
    summary_parts: list[str] = []
    high_elapsed = _get_rows(data, "high_elapsed_per_exec")
    high_exec = _get_rows(data, "high_execution_count")
    fts = _get_rows(data, "full_table_scans" if is_oracle else "seq_scan_tables")
    contention = _get_rows(data, "row_contention" if is_oracle else "lock_waits")
    seqs = _get_rows(
        data, "sequence_no_cache" if is_oracle else "sequence_cache_issues"
    )
    top_sql = (
        _get_rows(data, "top_cpu_sql" if is_oracle else "top_cpu_queries")
        or _get_rows(data, "awr_top_sql")
        or _get_rows(data, "pgprofile_top_sql")
    )

    if top_sql:
        summary_parts.append(f"{len(top_sql)} top SQL statements analysed")
    if high_elapsed:
        summary_parts.append(
            f"{len(high_elapsed)} queries with high elapsed time per execution"
        )
    if high_exec:
        summary_parts.append(
            f"{len(high_exec)} queries with very high execution counts"
        )
    if fts:
        summary_parts.append(
            f"{len(fts)} "
            f"{'full table scans' if is_oracle else 'tables with heavy seq scans'}"
        )
    if contention:
        summary_parts.append(f"{len(contention)} contention/lock wait events")
    if seqs:
        summary_parts.append(f"{len(seqs)} sequences with no/low caching")

    exec_summary = (
        "Found: " + "; ".join(summary_parts) + "."
        if summary_parts
        else "No significant performance issues detected in the collected data."
    )

    # Build final report: summary at top, then sections, then action plan
    header = [f"## Executive Summary\n{exec_summary}\n"]
    footer = ["\n## Action Plan (Priority Order)\n"]
    if action_items:
        footer.extend(action_items)
    else:
        footer.append(
            "No critical action items — database appears healthy based "
            "on collected data."
        )

    return "\n".join(header + parts + footer)


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
        # Fully programmatic analysis — Python code identifies all issues.
        # No LLM involved: codellama hallucinates generic advice.
        findings_report = _build_findings_report(raw_data)
        report_text = self._format_report(raw_data)

        return {
            "raw_data": raw_data,
            "report_text": report_text,
            "analysis": findings_report,
        }

    def _run_llm_analysis_from_text(self, report_text: str) -> dict[str, Any]:
        # For uploaded reports, we still need the LLM since we don't
        # have structured data — but we keep the prompt minimal.
        llm_prompt = (
            report_text + "\n\n---\n"
            "Summarise the key performance issues in the report above. "
            "Only reference data that actually appears above. "
            "Do NOT invent sql_ids, table names, or metrics."
        )
        try:
            llm_response = self.llm_client.generate(prompt=llm_prompt)
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
            "database_stats": _PG_DB_STATS,
            "bgwriter_stats": bgwriter_sql,
            "unused_indexes": _PG_UNUSED_INDEXES,
            "lock_waits": _PG_LOCK_WAITS,
            "bloat_estimate": _PG_BLOAT_ESTIMATE,
            "sequence_cache_issues": _PG_SEQUENCE_CACHE,
            "temp_file_usage": _PG_TEMP_FILE_USAGE,
            "connection_stats": _PG_CONNECTION_STATS,
            "checkpoint_stats": checkpoint_sql,
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
