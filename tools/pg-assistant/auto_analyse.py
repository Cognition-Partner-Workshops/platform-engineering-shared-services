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

ANALYSIS_SYSTEM_PROMPT = (
    "You are a senior DBA and database performance engineer performing a deep-dive "
    "analysis. You have been given detailed performance data including SQL IDs/query IDs, "
    "execution plans, full table scans, existing indexes, stats freshness, row contention "
    "events, sequence caching issues, and other best-practice metrics.\n\n"
    "Produce the following sections:\n\n"
    "## Executive Summary\n"
    "2-3 sentences summarising the overall database health and biggest concern.\n\n"
    "## High Elapsed Time SQL\n"
    "For EACH SQL with high average elapsed time per execution (reference sql_id/queryid):\n"
    "- Quote the sql_id / queryid, avg elapsed, total elapsed, and a snippet\n"
    "- Explain WHY it is slow (full table scan, missing index, bad join, bad stats)\n"
    "- Provide the EXACT fix SQL (CREATE INDEX, ANALYZE, rewrite, etc.)\n\n"
    "## High Execution Count SQL\n"
    "For SQL executed thousands of times:\n"
    "- Even small per-execution cost adds up; flag these with sql_id/queryid\n"
    "- Suggest caching, batching, or query consolidation where applicable\n"
    "- Provide exact fix SQL if index or rewrite would help\n\n"
    "## Full Table Scans\n"
    "List every table being full-scanned with the sql_id causing it.\n"
    "- For each, check the existing indexes section — if an index already exists "
    "that should have been used, suggest gathering fresh stats or checking predicates.\n"
    "- If no suitable index exists, provide the exact CREATE INDEX statement.\n\n"
    "## Row Contention & Locking Issues\n"
    "Analyse the row contention / enqueue wait events data:\n"
    "- Flag 'enq: TX - row lock contention' and similar events with wait times\n"
    "- Identify the likely cause (hot blocks, ITL contention, poor sequence caching)\n"
    "- Provide fixes: increase INITRANS, reduce transaction scope, batch commits\n\n"
    "## Sequence Caching Issues\n"
    "For sequences with NOCACHE or CACHE 1:\n"
    "- Explain the performance impact (row cache lock waits, redo contention)\n"
    "- Provide exact ALTER SEQUENCE ... CACHE 20 (or higher) statements\n"
    "- Flag ORDER sequences that may need NOORDER for better performance\n\n"
    "## Missing / Recommended Indexes\n"
    "Based on the query patterns (WHERE, JOIN, ORDER BY columns visible in SQL text), "
    "suggest specific CREATE INDEX statements. Reference the sql_id/queryid that "
    "would benefit.\n\n"
    "## Stale Statistics / Vacuum / Bloat Issues\n"
    "List tables with stale or missing stats. Provide exact ANALYZE / DBMS_STATS "
    "commands. For PostgreSQL, flag tables with high dead-tuple ratios needing VACUUM "
    "and estimate bloat. For Oracle, flag tables not analysed in 7+ days.\n\n"
    "## Temp Space / Sort Issues\n"
    "Flag queries spilling to temp (temp_blks_read/written for PG, sorts (disk) for "
    "Oracle). Suggest work_mem increase, index to avoid sort, or query rewrite.\n\n"
    "## Unused Indexes\n"
    "List indexes that have never been scanned and recommend dropping them "
    "(provide DROP INDEX statements).\n\n"
    "## Checkpoint / Redo / WAL Issues\n"
    "For Oracle: flag excessive redo log switches (>6/hour). "
    "For PostgreSQL: flag high backend_write_pct (buffers_backend vs checkpoint). "
    "Suggest redo log sizing or checkpoint_completion_target tuning.\n\n"
    "## Action Plan (Priority Order)\n"
    "Numbered list of actions sorted by impact. Each action must include:\n"
    "- The specific sql_id / queryid / table / sequence affected\n"
    "- The exact SQL command to execute\n"
    "- Expected improvement\n\n"
    "IMPORTANT RULES:\n"
    "1. Be SPECIFIC — always reference sql_id, queryid, or table name.\n"
    "2. Always QUOTE the full SQL text provided in the data alongside the sql_id/queryid. "
    "Show the complete query text so the reader can understand exactly which SQL is problematic.\n"
    "3. Never give generic advice like 'add indexes where needed'.\n"
    "4. Exclude all system/internal queries — focus only on user application SQL.\n"
    "5. Use markdown formatting with code blocks for SQL.\n"
    "6. For each problematic SQL, show it in a code block like:\n"
    "   ```sql\n"
    "   -- sql_id: ABC123\n"
    "   SELECT ... (full query text from the data)\n"
    "   ```\n"
    "7. Then explain the issue and provide the fix SQL in another code block."
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
        parts = [f"DATABASE PERFORMANCE REPORT ({db_type.upper()})\n{'=' * 60}\n"]

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
