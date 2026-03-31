"""Resource limit and session/process utilization monitoring."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig
from oracle_db_agent.connection import OracleConnectionManager

logger = logging.getLogger("oracle_db_agent.resource_monitor")

SQL_RESOURCE_LIMITS = """
SELECT
    resource_name,
    current_utilization,
    max_utilization,
    CASE
        WHEN limit_value = 'UNLIMITED' THEN -1
        ELSE TO_NUMBER(limit_value)
    END AS limit_value,
    limit_value AS limit_display
FROM v$resource_limit
WHERE resource_name IN (
    'sessions', 'processes', 'enqueue_locks', 'enqueue_resources',
    'transactions', 'max_shared_servers', 'parallel_max_servers'
)
ORDER BY resource_name
"""

SQL_SGA_USAGE = """
SELECT
    name,
    ROUND(value / 1024 / 1024, 2) AS mb
FROM v$sga
ORDER BY name
"""

SQL_PGA_USAGE = """
SELECT
    name,
    ROUND(value / 1024 / 1024, 2) AS mb
FROM v$pgastat
WHERE name IN (
    'total PGA allocated',
    'total PGA inuse',
    'maximum PGA allocated',
    'aggregate PGA target parameter',
    'aggregate PGA auto target'
)
ORDER BY name
"""

SQL_DB_PARAMETERS = """
SELECT name, value
FROM v$parameter
WHERE name IN (
    'sessions', 'processes', 'memory_target', 'memory_max_target',
    'sga_target', 'sga_max_size', 'pga_aggregate_target',
    'pga_aggregate_limit', 'open_cursors', 'db_files'
)
ORDER BY name
"""


@dataclass
class ResourceLimit:
    resource_name: str
    current_utilization: int
    max_utilization: int
    limit_value: int  # -1 means UNLIMITED
    limit_display: str
    pct_used: float


class ResourceMonitor:
    """Monitors database resource limits and memory utilization."""

    def __init__(
        self,
        config: AgentConfig,
        conn_mgr: OracleConnectionManager,
        alerter: EmailAlerter,
    ) -> None:
        self._config = config
        self._conn = conn_mgr
        self._alerter = alerter
        self._threshold = config.thresholds.session_utilization_warning_pct

    def check_resource_limits(self) -> list[ResourceLimit]:
        """Check current resource utilization against limits."""
        logger.info("Checking resource limits...")
        rows = self._conn.execute_query(SQL_RESOURCE_LIMITS)

        results: list[ResourceLimit] = []
        for row in rows:
            limit_val = int(row[3])
            current = int(row[1])
            pct = (current / limit_val * 100) if limit_val > 0 else 0.0

            rl = ResourceLimit(
                resource_name=row[0],
                current_utilization=current,
                max_utilization=int(row[2]),
                limit_value=limit_val,
                limit_display=str(row[4]),
                pct_used=pct,
            )
            results.append(rl)

            if pct >= self._threshold:
                logger.warning(
                    "Resource %s at %.1f%% (%d/%s)",
                    rl.resource_name, pct, current, rl.limit_display,
                )
            else:
                logger.info(
                    "Resource %s: %d/%s (%.1f%%)",
                    rl.resource_name, current, rl.limit_display, pct,
                )

        return results

    def check_memory_usage(self) -> dict[str, list[tuple[str, float]]]:
        """Check SGA and PGA memory usage."""
        sga_rows = self._conn.execute_query(SQL_SGA_USAGE)
        pga_rows = self._conn.execute_query(SQL_PGA_USAGE)
        return {
            "sga": [(str(r[0]), float(r[1])) for r in sga_rows],
            "pga": [(str(r[0]), float(r[1])) for r in pga_rows],
        }

    def monitor_and_alert(self) -> list[ResourceLimit]:
        """Check resource limits and alert on threshold violations."""
        resources = self.check_resource_limits()
        alerts = [r for r in resources if r.pct_used >= self._threshold]

        if alerts:
            memory = self.check_memory_usage()
            self._send_resource_alert(resources, alerts, memory)

        return resources

    def _send_resource_alert(
        self,
        all_resources: list[ResourceLimit],
        alerts: list[ResourceLimit],
        memory: dict[str, list[tuple[str, float]]],
    ) -> None:
        """Send email alert for resource limit issues."""
        resource_html = ""
        for r in all_resources:
            color = "#ff4444" if r.pct_used >= 90 else ("#ff8800" if r.pct_used >= self._threshold else "#333")
            resource_html += (
                f"<tr><td>{r.resource_name}</td>"
                f"<td style='color:{color};font-weight:bold'>{r.current_utilization}</td>"
                f"<td>{r.max_utilization}</td>"
                f"<td>{r.limit_display}</td>"
                f"<td style='color:{color}'>{r.pct_used:.1f}%</td></tr>"
            )

        sga_html = ""
        for name, mb in memory.get("sga", []):
            sga_html += f"<tr><td>{name}</td><td>{mb:.1f} MB</td></tr>"

        pga_html = ""
        for name, mb in memory.get("pga", []):
            pga_html += f"<tr><td>{name}</td><td>{mb:.1f} MB</td></tr>"

        html = f"""
        <h3>Resource Limit Alert</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><th>Resource</th><th>Current</th><th>Max Used</th><th>Limit</th><th>% Used</th></tr>
            {resource_html}
        </table>

        <h4>SGA Memory:</h4>
        <table border="1" cellpadding="6" cellspacing="0">
            {sga_html}
        </table>

        <h4>PGA Memory:</h4>
        <table border="1" cellpadding="6" cellspacing="0">
            {pga_html}
        </table>
        """
        self._alerter.send_alert(
            subject=f"Resource Limit Warning ({len(alerts)} resources above threshold)",
            body_html=html,
        )

    def get_report(self) -> str:
        """Generate a text report of resource utilization."""
        resources = self.check_resource_limits()
        memory = self.check_memory_usage()

        lines = [
            "=" * 85,
            f"{'RESOURCE UTILIZATION REPORT':^85}",
            "=" * 85,
            f"{'Resource':<25} {'Current':>10} {'Max Used':>10} {'Limit':>12} {'% Used':>10}",
            "-" * 85,
        ]
        for r in resources:
            status = " ***" if r.pct_used >= self._threshold else ""
            lines.append(
                f"{r.resource_name:<25} {r.current_utilization:>10} {r.max_utilization:>10} "
                f"{r.limit_display:>12} {r.pct_used:>9.1f}%{status}"
            )

        lines.append("")
        lines.append(f"{'SGA Memory':^85}")
        lines.append("-" * 85)
        for name, mb in memory.get("sga", []):
            lines.append(f"  {name:<40} {mb:>10.1f} MB")

        lines.append("")
        lines.append(f"{'PGA Memory':^85}")
        lines.append("-" * 85)
        for name, mb in memory.get("pga", []):
            lines.append(f"  {name:<40} {mb:>10.1f} MB")

        lines.append("=" * 85)
        return "\n".join(lines)
