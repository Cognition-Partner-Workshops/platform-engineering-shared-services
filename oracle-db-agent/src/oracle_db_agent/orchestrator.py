"""Agent orchestrator — coordinates all modules for pre-run, post-run, and monitoring."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.approval import ApprovalManager
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager
from oracle_db_agent.modules.alert_log_monitor import AlertLogMonitor
from oracle_db_agent.modules.auto_start import AutoStartManager
from oracle_db_agent.modules.awr_manager import AWRManager
from oracle_db_agent.modules.index_health import IndexHealthChecker
from oracle_db_agent.modules.invalid_objects import InvalidObjectRecompiler
from oracle_db_agent.modules.object_auditor import ObjectAuditor
from oracle_db_agent.modules.resource_monitor import ResourceMonitor
from oracle_db_agent.modules.session_monitor import SessionMonitor
from oracle_db_agent.modules.stats_collector import StatsCollector
from oracle_db_agent.modules.tablespace_manager import TablespaceManager
from oracle_db_agent.modules.temp_monitor import TempMonitor
from oracle_db_agent.modules.undo_monitor import UndoMonitor

logger = logging.getLogger("oracle_db_agent.orchestrator")


class AgentOrchestrator:
    """Central orchestrator that coordinates all agent modules.

    Supports three primary modes:
    - pre-run:  Tasks to execute before a performance test
    - post-run: Tasks to execute after a performance test
    - monitor:  General monitoring/maintenance tasks
    """

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._conn = OracleConnectionManager(config)
        self._alerter = EmailAlerter(config)
        self._approval = ApprovalManager(config, self._alerter)

        # Initialize all modules
        self._tablespace = TablespaceManager(config, self._conn, self._alerter, self._approval)
        self._object_auditor = ObjectAuditor(config, self._conn, self._alerter)
        self._stats_collector = StatsCollector(config, self._conn, self._alerter)
        self._auto_start = AutoStartManager(config, self._alerter)
        self._awr = AWRManager(config, self._conn, self._alerter)
        self._temp = TempMonitor(config, self._conn, self._alerter)
        self._undo = UndoMonitor(config, self._conn, self._alerter)
        self._session = SessionMonitor(config, self._conn, self._alerter, self._approval)
        self._alert_log = AlertLogMonitor(config, self._conn, self._alerter)
        self._invalid_obj = InvalidObjectRecompiler(config, self._conn, self._alerter)
        self._resource = ResourceMonitor(config, self._conn, self._alerter)
        self._index = IndexHealthChecker(config, self._conn, self._alerter, self._approval)

        # Store pre-run AWR snapshot for post-run report generation
        self._pre_run_snap_id: int | None = None

        # Task registry: maps task names to handler methods
        self._task_registry: dict[str, Any] = {
            "check_db_status": self._task_check_db_status,
            "check_listener": self._task_check_listener,
            "check_tablespace": self._task_check_tablespace,
            "gather_stale_stats": self._task_gather_stale_stats,
            "recompile_invalid_objects": self._task_recompile_invalid,
            "check_resource_limits": self._task_check_resource_limits,
            "check_non_system_objects": self._task_check_non_system_objects,
            "check_index_health": self._task_check_index_health,
            "clear_temp_segments": self._task_clear_temp,
            "awr_snapshot": self._task_awr_snapshot,
            "generate_awr_report": self._task_generate_awr_report,
            "check_alert_log": self._task_check_alert_log,
            "report_long_sessions": self._task_report_long_sessions,
            "report_temp_usage": self._task_report_temp_usage,
            "report_tablespace_growth": self._task_report_tablespace_growth,
            "check_undo": self._task_check_undo,
        }

    def initialize(self) -> bool:
        """Initialize connection pool and verify connectivity."""
        try:
            self._conn.initialize_pool()
            if self._conn.test_connection():
                logger.info("Database connection verified.")
                return True
            else:
                logger.error("Database connection test failed.")
                return False
        except Exception as exc:
            logger.error("Failed to initialize: %s", exc)
            return False

    def shutdown(self) -> None:
        """Clean up resources."""
        self._conn.close_pool()
        logger.info("Agent shutdown complete.")

    # -----------------------------------------------------------------------
    # Primary Execution Modes
    # -----------------------------------------------------------------------

    def run_pre_run(self, force_stats: bool = False) -> dict[str, str]:
        """Execute all pre-performance-test tasks.

        Returns a dict of task_name -> result_summary.
        """
        logger.info("=" * 70)
        logger.info("STARTING PRE-RUN CHECKS")
        logger.info("=" * 70)

        results: dict[str, str] = {}
        tasks = self._config.performance_testing.pre_run_tasks

        for task_name in tasks:
            handler = self._task_registry.get(task_name)
            if handler is None:
                logger.warning("Unknown task: %s (skipping)", task_name)
                results[task_name] = "SKIPPED (unknown task)"
                continue

            try:
                logger.info("--- Running task: %s ---", task_name)
                result = handler(force=force_stats) if task_name == "gather_stale_stats" else handler()
                results[task_name] = result
                logger.info("Task %s: %s", task_name, result)
            except Exception as exc:
                results[task_name] = f"ERROR: {exc}"
                logger.error("Task %s failed: %s", task_name, exc)

        logger.info("=" * 70)
        logger.info("PRE-RUN CHECKS COMPLETE")
        logger.info("=" * 70)

        # Send consolidated email report
        self._send_run_report("Pre-Run", results)
        return results

    def run_post_run(self) -> dict[str, str]:
        """Execute all post-performance-test tasks.

        Returns a dict of task_name -> result_summary.
        """
        logger.info("=" * 70)
        logger.info("STARTING POST-RUN CHECKS")
        logger.info("=" * 70)

        results: dict[str, str] = {}
        tasks = self._config.performance_testing.post_run_tasks

        for task_name in tasks:
            handler = self._task_registry.get(task_name)
            if handler is None:
                logger.warning("Unknown task: %s (skipping)", task_name)
                results[task_name] = "SKIPPED (unknown task)"
                continue

            try:
                logger.info("--- Running task: %s ---", task_name)
                result = handler()
                results[task_name] = result
                logger.info("Task %s: %s", task_name, result)
            except Exception as exc:
                results[task_name] = f"ERROR: {exc}"
                logger.error("Task %s failed: %s", task_name, exc)

        logger.info("=" * 70)
        logger.info("POST-RUN CHECKS COMPLETE")
        logger.info("=" * 70)

        self._send_run_report("Post-Run", results)
        return results

    def run_monitor(self) -> dict[str, str]:
        """Run a full monitoring pass (all health checks)."""
        logger.info("=" * 70)
        logger.info("STARTING MONITORING PASS")
        logger.info("=" * 70)

        results: dict[str, str] = {}

        monitor_tasks = [
            "check_tablespace",
            "check_non_system_objects",
            "check_resource_limits",
            "check_index_health",
            "check_undo",
            "report_temp_usage",
            "check_alert_log",
            "report_long_sessions",
        ]

        for task_name in monitor_tasks:
            handler = self._task_registry.get(task_name)
            if handler is None:
                continue
            try:
                result = handler()
                results[task_name] = result
            except Exception as exc:
                results[task_name] = f"ERROR: {exc}"
                logger.error("Monitor task %s failed: %s", task_name, exc)

        logger.info("MONITORING PASS COMPLETE")
        return results

    def run_auto_start(self) -> bool:
        """Run the auto-start sequence (post-reboot)."""
        return self._auto_start.auto_start()

    # -----------------------------------------------------------------------
    # Individual Task Handlers
    # -----------------------------------------------------------------------

    def _task_check_db_status(self) -> str:
        status = self._auto_start.check_db_status()
        if status == "OPEN":
            return "OK: Database is OPEN"
        return f"WARNING: Database status is {status}"

    def _task_check_listener(self) -> str:
        running = self._auto_start.check_listener_status()
        if running:
            return "OK: Listener is running"
        return "WARNING: Listener is NOT running"

    def _task_check_tablespace(self) -> str:
        actions = self._tablespace.auto_manage()
        ts_list = self._tablespace.check_tablespace_usage()
        critical = [t for t in ts_list if t.pct_used >= self._config.thresholds.tablespace_critical_pct]
        warn_pct = self._config.thresholds.tablespace_warning_pct
        crit_pct = self._config.thresholds.tablespace_critical_pct
        warning = [t for t in ts_list if warn_pct <= t.pct_used < crit_pct]
        summary = f"OK: {len(ts_list)} tablespaces checked"
        if critical:
            crit = self._config.thresholds.tablespace_critical_pct
            summary = f"CRITICAL: {len(critical)} tablespace(s) above {crit}%"
        elif warning:
            summary = f"WARNING: {len(warning)} tablespace(s) above {self._config.thresholds.tablespace_warning_pct}%"
        if actions:
            summary += f"; Actions taken: {len(actions)}"
        return summary

    def _task_gather_stale_stats(self, force: bool = False) -> str:
        count = self._stats_collector.gather_stale_stats(force=force)
        if count > 0:
            return f"OK: Stats gathered for {count} schema(s)"
        return "SKIPPED: Outside maintenance window (use --force to override)"

    def _task_recompile_invalid(self) -> str:
        objects = self._invalid_obj.find_invalid_objects()
        if not objects:
            return "OK: No invalid objects found"
        success = self._invalid_obj.recompile_all(
            parallel_degree=self._config.stats_collection.parallel_degree
        )
        after = self._invalid_obj.find_invalid_objects()
        return f"{'OK' if success else 'WARNING'}: {len(objects)} invalid -> {len(after)} remaining"

    def _task_check_resource_limits(self) -> str:
        resources = self._resource.check_resource_limits()
        alerts = [r for r in resources if r.pct_used >= self._config.thresholds.session_utilization_warning_pct]
        if alerts:
            names = ", ".join(r.resource_name for r in alerts)
            return f"WARNING: Resources near limit: {names}"
        return "OK: All resources within limits"

    def _task_check_non_system_objects(self) -> str:
        objects = self._object_auditor.find_non_system_objects()
        if objects:
            total_mb = sum(o.size_mb for o in objects)
            return f"WARNING: {len(objects)} non-system objects in SYSTEM/SYSAUX ({total_mb:.1f} MB)"
        return "OK: No non-system objects in SYSTEM/SYSAUX"

    def _task_check_index_health(self) -> str:
        indexes = self._index.check_index_health()
        unusable = self._index.find_unusable_indexes()
        total = len(indexes) + len(unusable)
        if total > 0:
            return f"WARNING: {total} indexes need attention ({len(unusable)} unusable)"
        return "OK: All indexes healthy"

    def _task_clear_temp(self) -> str:
        usage = self._temp.check_temp_usage()
        if usage:
            high = [u for u in usage if u.pct_used >= self._config.thresholds.temp_warning_pct]
            if high:
                return f"WARNING: Temp tablespace {high[0].tablespace_name} at {high[0].pct_used:.1f}%"
        return "OK: Temp tablespace usage normal"

    def _task_awr_snapshot(self) -> str:
        snap_id = self._awr.create_snapshot()
        if snap_id:
            # Store for post-run report generation
            if self._pre_run_snap_id is None:
                self._pre_run_snap_id = snap_id
            return f"OK: AWR snapshot created (snap_id={snap_id})"
        return "ERROR: Failed to create AWR snapshot"

    def _task_generate_awr_report(self) -> str:
        end_snap = self._awr.get_latest_snapshot_id()
        if end_snap is None:
            return "ERROR: No AWR snapshots found"

        begin_snap = self._pre_run_snap_id
        if begin_snap is None:
            # Fall back to second-most-recent snapshot
            begin_snap = end_snap - 1

        if begin_snap >= end_snap:
            return "SKIPPED: Need at least 2 snapshots for report"

        awr_path, addm_path = self._awr.generate_reports_for_window(begin_snap, end_snap)
        parts = []
        if awr_path:
            parts.append(f"AWR: {awr_path}")
        if addm_path:
            parts.append(f"ADDM: {addm_path}")
        return f"OK: {'; '.join(parts)}" if parts else "ERROR: Report generation failed"

    def _task_check_alert_log(self) -> str:
        summary = self._alert_log.scan_alert_log(hours=24)
        if summary.critical_count > 0:
            return f"CRITICAL: {summary.critical_count} critical errors in alert log"
        if summary.warning_count > 0:
            return f"WARNING: {summary.warning_count} warning errors in alert log"
        return f"OK: {summary.total_entries} entries, no critical/warning errors"

    def _task_report_long_sessions(self) -> str:
        queries = self._session.find_long_running_queries()
        blocking = self._session.find_blocking_sessions()
        parts = []
        if queries:
            parts.append(f"{len(queries)} long-running queries")
        if blocking:
            parts.append(f"{len(blocking)} blocking chains")
        if parts:
            return f"WARNING: {', '.join(parts)}"
        return "OK: No long-running queries or blocking sessions"

    def _task_report_temp_usage(self) -> str:
        usage = self._temp.check_temp_usage()
        if usage:
            max_usage = max(usage, key=lambda u: u.pct_used)
            if max_usage.pct_used >= self._config.thresholds.temp_warning_pct:
                return f"WARNING: Temp {max_usage.tablespace_name} at {max_usage.pct_used:.1f}%"
            return f"OK: Temp usage normal (max {max_usage.pct_used:.1f}%)"
        return "OK: No temp usage data"

    def _task_report_tablespace_growth(self) -> str:
        ts_list = self._tablespace.check_tablespace_usage()
        critical = [t for t in ts_list if t.pct_used >= self._config.thresholds.tablespace_critical_pct]
        if critical:
            names = ", ".join(f"{t.name}({t.pct_used:.1f}%)" for t in critical)
            return f"CRITICAL: Tablespace growth alert: {names}"
        return f"OK: {len(ts_list)} tablespaces within limits"

    def _task_check_undo(self) -> str:
        ts_info = self._undo.check_undo_tablespace()
        ora_01555 = self._undo.check_ora_01555_risk()
        parts = [f"Undo {ts_info.tablespace_name}: {ts_info.pct_used:.1f}% used"]
        if ora_01555:
            parts.append("ORA-01555 RISK DETECTED")
            return f"WARNING: {'; '.join(parts)}"
        return f"OK: {'; '.join(parts)}"

    # -----------------------------------------------------------------------
    # Report Generation
    # -----------------------------------------------------------------------

    def generate_full_report(self) -> str:
        """Generate a comprehensive text report from all modules."""
        sections = [
            self._auto_start.get_status_report(),
            self._tablespace.get_report(),
            self._object_auditor.get_report(),
            self._stats_collector.get_report(),
            self._temp.get_report(),
            self._undo.get_report(),
            self._session.get_report(),
            self._resource.get_report(),
            self._index.get_report(),
            self._alert_log.get_report(),
            self._awr.get_report(),
        ]
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"\n{'=' * 90}\n{'ORACLE DB AGENT - FULL REPORT':^90}\n{timestamp:^90}\n{'=' * 90}\n"
        return header + "\n\n".join(sections)

    def _send_run_report(self, run_type: str, results: dict[str, str]) -> None:
        """Send consolidated email report for pre/post-run."""
        rows_html = ""
        has_errors = False
        for task, result in results.items():
            if result.startswith("ERROR") or result.startswith("CRITICAL"):
                color = "#ff4444"
                has_errors = True
            elif result.startswith("WARNING"):
                color = "#ff8800"
            elif result.startswith("SKIPPED"):
                color = "#999"
            else:
                color = "#228B22"
            rows_html += (
                f"<tr><td>{task}</td>"
                f"<td style='color:{color}'>{result}</td></tr>"
            )

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html = f"""
        <h3>{run_type} Report - {timestamp}</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Task</th><th>Result</th></tr>
            {rows_html}
        </table>
        """

        status = "ALERT" if has_errors else "OK"
        self._alerter.send_alert(
            subject=f"{status}: {run_type} Report ({len(results)} tasks)",
            body_html=html,
        )

    # -----------------------------------------------------------------------
    # Module Accessors (for CLI direct usage)
    # -----------------------------------------------------------------------

    @property
    def tablespace(self) -> TablespaceManager:
        return self._tablespace

    @property
    def object_auditor(self) -> ObjectAuditor:
        return self._object_auditor

    @property
    def stats_collector(self) -> StatsCollector:
        return self._stats_collector

    @property
    def auto_start_manager(self) -> AutoStartManager:
        return self._auto_start

    @property
    def awr(self) -> AWRManager:
        return self._awr

    @property
    def temp(self) -> TempMonitor:
        return self._temp

    @property
    def undo(self) -> UndoMonitor:
        return self._undo

    @property
    def session(self) -> SessionMonitor:
        return self._session

    @property
    def alert_log(self) -> AlertLogMonitor:
        return self._alert_log

    @property
    def invalid_objects(self) -> InvalidObjectRecompiler:
        return self._invalid_obj

    @property
    def resource_monitor(self) -> ResourceMonitor:
        return self._resource

    @property
    def index_health(self) -> IndexHealthChecker:
        return self._index

    @property
    def approval_manager(self) -> ApprovalManager:
        return self._approval
