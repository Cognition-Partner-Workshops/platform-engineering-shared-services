"""Session and lock monitoring for Oracle and PostgreSQL.

Provides live views of:
- Active sessions and their current SQL
- Blocking lock trees (who is blocking whom)
- Wait chains
- Long-running queries
"""

import logging
from typing import Any

from db_client import BaseDBClient, DB_TYPE_ORACLE

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Oracle session / lock queries
# ---------------------------------------------------------------------------
_ORA_ACTIVE_SESSIONS = """
    SELECT
        s.sid,
        s.serial#  AS serial_num,
        s.username,
        s.status,
        s.osuser,
        s.machine,
        s.program,
        s.wait_class,
        s.event,
        s.seconds_in_wait,
        s.sql_id,
        SUBSTR(q.sql_text, 1, 200) AS sql_text,
        s.blocking_session,
        s.blocking_session_status
    FROM v$session s
    LEFT JOIN v$sql q ON s.sql_id = q.sql_id AND s.sql_child_number = q.child_number
    WHERE s.type = 'USER'
        AND s.status = 'ACTIVE'
    ORDER BY s.seconds_in_wait DESC
"""

_ORA_BLOCKING_TREE = """
    SELECT
        LPAD(' ', 2 * (LEVEL - 1)) || s.sid || ',' || s.serial# AS session_id,
        s.username,
        s.status,
        s.sql_id,
        SUBSTR(q.sql_text, 1, 200) AS sql_text,
        s.event,
        s.seconds_in_wait,
        s.blocking_session,
        l.type AS lock_type,
        DECODE(l.lmode,
            0, 'None', 1, 'Null', 2, 'Row-S', 3, 'Row-X',
            4, 'Share', 5, 'S/Row-X', 6, 'Exclusive', l.lmode) AS lock_mode,
        DECODE(l.request,
            0, 'None', 1, 'Null', 2, 'Row-S', 3, 'Row-X',
            4, 'Share', 5, 'S/Row-X', 6, 'Exclusive', l.request) AS lock_request
    FROM v$session s
    LEFT JOIN v$sql q ON s.sql_id = q.sql_id AND s.sql_child_number = q.child_number
    LEFT JOIN v$lock l ON s.sid = l.sid AND l.block > 0
    START WITH s.blocking_session IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM v$session s2
            WHERE s2.sid = s.blocking_session
                AND s2.blocking_session IS NOT NULL
        )
    CONNECT BY PRIOR s.sid = s.blocking_session
    ORDER SIBLINGS BY s.seconds_in_wait DESC
"""

_ORA_LOCK_DETAILS = """
    SELECT
        l.sid,
        s.serial# AS serial_num,
        s.username,
        l.type AS lock_type,
        DECODE(l.lmode,
            0, 'None', 1, 'Null', 2, 'Row-S', 3, 'Row-X',
            4, 'Share', 5, 'S/Row-X', 6, 'Exclusive', l.lmode) AS lock_mode,
        DECODE(l.request,
            0, 'None', 1, 'Null', 2, 'Row-S', 3, 'Row-X',
            4, 'Share', 5, 'S/Row-X', 6, 'Exclusive', l.request) AS lock_request,
        l.block,
        o.object_name,
        o.object_type,
        s.sql_id,
        SUBSTR(q.sql_text, 1, 200) AS sql_text
    FROM v$lock l
    JOIN v$session s ON l.sid = s.sid
    LEFT JOIN dba_objects o ON l.id1 = o.object_id
    LEFT JOIN v$sql q ON s.sql_id = q.sql_id AND s.sql_child_number = q.child_number
    WHERE l.type NOT IN ('AE', 'PS')
        AND (l.block > 0 OR l.request > 0)
    ORDER BY l.block DESC, l.request DESC
"""

_ORA_LONG_RUNNING = """
    SELECT * FROM (
        SELECT
            s.sid,
            s.serial# AS serial_num,
            s.username,
            s.sql_id,
            SUBSTR(q.sql_text, 1, 200) AS sql_text,
            ROUND(s.last_call_et) AS running_sec,
            s.event,
            s.wait_class,
            s.program,
            s.machine
        FROM v$session s
        LEFT JOIN v$sql q ON s.sql_id = q.sql_id
            AND s.sql_child_number = q.child_number
        WHERE s.type = 'USER'
            AND s.status = 'ACTIVE'
            AND s.last_call_et > 5
        ORDER BY s.last_call_et DESC
    ) WHERE ROWNUM <= 30
"""

_ORA_WAIT_CHAINS = """
    SELECT
        s.sid,
        s.serial# AS serial_num,
        s.username,
        s.event,
        s.wait_class,
        s.seconds_in_wait,
        s.blocking_session,
        s.sql_id
    FROM v$session s
    WHERE s.type = 'USER'
        AND s.wait_class != 'Idle'
        AND s.seconds_in_wait > 1
    ORDER BY s.seconds_in_wait DESC
"""

# ---------------------------------------------------------------------------
# PostgreSQL session / lock queries
# ---------------------------------------------------------------------------
_PG_ACTIVE_SESSIONS = """
    SELECT
        pid,
        usename,
        datname,
        client_addr::text,
        application_name,
        state,
        wait_event_type,
        wait_event,
        LEFT(query, 300) AS query,
        ROUND(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 1) AS running_sec,
        ROUND(EXTRACT(EPOCH FROM (now() - backend_start))::numeric, 0) AS session_age_sec
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
        AND state != 'idle'
    ORDER BY query_start
"""

_PG_BLOCKING_TREE = """
    WITH RECURSIVE lock_tree AS (
        SELECT
            blocked.pid AS blocked_pid,
            blocked.usename AS blocked_user,
            LEFT(blocked.query, 200) AS blocked_query,
            blocked.wait_event_type,
            blocked.wait_event,
            blocking.pid AS blocking_pid,
            blocking.usename AS blocking_user,
            LEFT(blocking.query, 200) AS blocking_query,
            1 AS depth
        FROM pg_stat_activity blocked
        JOIN pg_locks bl ON bl.pid = blocked.pid
        JOIN pg_locks kl ON kl.locktype = bl.locktype
            AND kl.database IS NOT DISTINCT FROM bl.database
            AND kl.relation IS NOT DISTINCT FROM bl.relation
            AND kl.page IS NOT DISTINCT FROM bl.page
            AND kl.tuple IS NOT DISTINCT FROM bl.tuple
            AND kl.virtualxid IS NOT DISTINCT FROM bl.virtualxid
            AND kl.transactionid IS NOT DISTINCT FROM bl.transactionid
            AND kl.classid IS NOT DISTINCT FROM bl.classid
            AND kl.objid IS NOT DISTINCT FROM bl.objid
            AND kl.objsubid IS NOT DISTINCT FROM bl.objsubid
            AND kl.pid != bl.pid
        JOIN pg_stat_activity blocking ON kl.pid = blocking.pid
        WHERE NOT bl.granted AND kl.granted
    )
    SELECT DISTINCT
        blocked_pid,
        blocked_user,
        blocked_query,
        wait_event_type,
        wait_event,
        blocking_pid,
        blocking_user,
        blocking_query,
        depth
    FROM lock_tree
    ORDER BY blocking_pid, depth
"""

_PG_LOCK_DETAILS = """
    SELECT
        l.pid,
        a.usename,
        l.locktype,
        l.mode,
        l.granted,
        l.relation::regclass::text AS locked_relation,
        LEFT(a.query, 200) AS query,
        a.state,
        ROUND(EXTRACT(EPOCH FROM (now() - a.query_start))::numeric, 1) AS query_sec
    FROM pg_locks l
    JOIN pg_stat_activity a ON l.pid = a.pid
    WHERE l.pid != pg_backend_pid()
        AND l.relation IS NOT NULL
    ORDER BY l.granted, a.query_start
"""

_PG_LONG_RUNNING = """
    SELECT
        pid,
        usename,
        datname,
        LEFT(query, 300) AS query,
        state,
        wait_event_type,
        wait_event,
        ROUND(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 1) AS running_sec,
        application_name,
        client_addr::text
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
        AND state = 'active'
        AND query_start < now() - interval '5 seconds'
    ORDER BY query_start
    LIMIT 30
"""

_PG_WAIT_EVENTS = """
    SELECT
        pid,
        usename,
        wait_event_type,
        wait_event,
        state,
        LEFT(query, 200) AS query,
        ROUND(EXTRACT(EPOCH FROM (now() - query_start))::numeric, 1) AS running_sec
    FROM pg_stat_activity
    WHERE pid != pg_backend_pid()
        AND wait_event IS NOT NULL
        AND state != 'idle'
    ORDER BY query_start
"""


# ---------------------------------------------------------------------------
# Kill session queries
# ---------------------------------------------------------------------------
_ORA_KILL_SESSION = "ALTER SYSTEM KILL SESSION '{sid},{serial_num}' IMMEDIATE"

_PG_CANCEL_QUERY = "SELECT pg_cancel_backend({pid})"
_PG_TERMINATE_BACKEND = "SELECT pg_terminate_backend({pid})"


# ---------------------------------------------------------------------------
# SessionMonitor class
# ---------------------------------------------------------------------------
class SessionMonitor:
    """Collects session and lock information from Oracle or PostgreSQL."""

    def __init__(self, db_client: BaseDBClient) -> None:
        self.db_client = db_client

    def get_active_sessions(self) -> dict[str, Any]:
        """Return active (non-idle) sessions."""
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self.db_client.execute_query(_ORA_ACTIVE_SESSIONS)
        return self.db_client.execute_query(_PG_ACTIVE_SESSIONS)

    def get_blocking_tree(self) -> dict[str, Any]:
        """Return blocking lock tree (who blocks whom)."""
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self.db_client.execute_query(_ORA_BLOCKING_TREE)
        return self.db_client.execute_query(_PG_BLOCKING_TREE)

    def get_lock_details(self) -> dict[str, Any]:
        """Return detailed lock information."""
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self.db_client.execute_query(_ORA_LOCK_DETAILS)
        return self.db_client.execute_query(_PG_LOCK_DETAILS)

    def get_long_running(self) -> dict[str, Any]:
        """Return long-running queries (>5 seconds)."""
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self.db_client.execute_query(_ORA_LONG_RUNNING)
        return self.db_client.execute_query(_PG_LONG_RUNNING)

    def get_wait_events(self) -> dict[str, Any]:
        """Return sessions currently waiting."""
        if self.db_client.db_type == DB_TYPE_ORACLE:
            return self.db_client.execute_query(_ORA_WAIT_CHAINS)
        return self.db_client.execute_query(_PG_WAIT_EVENTS)

    def kill_session(
        self, pid_or_sid: int, serial_num: int = 0, force: bool = False
    ) -> dict[str, Any]:
        """Kill/cancel a session.

        Oracle: ALTER SYSTEM KILL SESSION 'sid,serial#' IMMEDIATE
        PostgreSQL: pg_cancel_backend (soft) or pg_terminate_backend (force)
        """
        if self.db_client.db_type == DB_TYPE_ORACLE:
            sql = _ORA_KILL_SESSION.format(sid=pid_or_sid, serial_num=serial_num)
            return self.db_client.execute_statement(sql)
        if force:
            sql = _PG_TERMINATE_BACKEND.format(pid=pid_or_sid)
        else:
            sql = _PG_CANCEL_QUERY.format(pid=pid_or_sid)
        return self.db_client.execute_query(sql)
