"""Auto-start Oracle database and listener after reboot."""

from __future__ import annotations

import logging
import os
import subprocess
import time

from oracle_db_agent.alerting import EmailAlerter
from oracle_db_agent.config import AgentConfig

logger = logging.getLogger("oracle_db_agent.auto_start")


class AutoStartManager:
    """Detects and starts Oracle database and listener if they are down.

    Designed for Oracle 19c traditional (non-CDB) architecture.
    Uses sqlplus and lsnrctl via subprocess for startup operations.
    """

    def __init__(self, config: AgentConfig, alerter: EmailAlerter) -> None:
        self._config = config
        self._alerter = alerter
        self._oracle_home = config.oracle_env.oracle_home
        self._oracle_sid = config.oracle_env.oracle_sid
        self._oracle_base = config.oracle_env.oracle_base
        self._max_retries = config.auto_start.max_retries
        self._retry_delay = config.auto_start.retry_delay_seconds

    def _get_env(self) -> dict[str, str]:
        """Get environment variables for Oracle commands."""
        env = os.environ.copy()
        env["ORACLE_HOME"] = self._oracle_home
        env["ORACLE_SID"] = self._oracle_sid
        env["ORACLE_BASE"] = self._oracle_base
        env["PATH"] = f"{self._oracle_home}/bin:{env.get('PATH', '')}"
        env["LD_LIBRARY_PATH"] = f"{self._oracle_home}/lib:{env.get('LD_LIBRARY_PATH', '')}"
        return env

    def _run_sqlplus(self, commands: str, as_sysdba: bool = True) -> tuple[int, str, str]:
        """Execute SQL commands via sqlplus."""
        sqlplus_path = os.path.join(self._oracle_home, "bin", "sqlplus")
        connect_str = "/ as sysdba" if as_sysdba else "/"

        full_cmd = f"{commands}\nEXIT;\n"
        try:
            result = subprocess.run(
                [sqlplus_path, "-S", connect_str],
                input=full_cmd,
                capture_output=True,
                text=True,
                env=self._get_env(),
                timeout=120,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "sqlplus command timed out"
        except FileNotFoundError:
            return -1, "", f"sqlplus not found at {sqlplus_path}"

    def _run_lsnrctl(self, command: str) -> tuple[int, str, str]:
        """Execute lsnrctl commands."""
        lsnrctl_path = os.path.join(self._oracle_home, "bin", "lsnrctl")
        try:
            result = subprocess.run(
                [lsnrctl_path, command],
                capture_output=True,
                text=True,
                env=self._get_env(),
                timeout=60,
            )
            return result.returncode, result.stdout, result.stderr
        except subprocess.TimeoutExpired:
            return -1, "", "lsnrctl command timed out"
        except FileNotFoundError:
            return -1, "", f"lsnrctl not found at {lsnrctl_path}"

    def check_db_status(self) -> str:
        """Check the database instance status.

        Returns one of: 'OPEN', 'MOUNTED', 'STARTED', 'SHUTDOWN', 'UNKNOWN'
        """
        rc, stdout, stderr = self._run_sqlplus(
            "SELECT status FROM v$instance;"
        )
        if rc != 0 or "ORA-01034" in stdout or "ORA-01034" in stderr:
            logger.info("Database appears to be SHUTDOWN.")
            return "SHUTDOWN"

        stdout_upper = stdout.upper()
        if "OPEN" in stdout_upper:
            return "OPEN"
        if "MOUNTED" in stdout_upper:
            return "MOUNTED"
        if "STARTED" in stdout_upper:
            return "STARTED"

        logger.warning("Unable to determine DB status. stdout=%s, stderr=%s", stdout[:200], stderr[:200])
        return "UNKNOWN"

    def check_listener_status(self) -> bool:
        """Check if the Oracle listener is running."""
        rc, stdout, _ = self._run_lsnrctl("status")
        return bool(rc == 0 and "The command completed successfully" in stdout)

    def start_database(self) -> bool:
        """Start the Oracle database instance and open it."""
        status = self.check_db_status()

        if status == "OPEN":
            logger.info("Database is already OPEN.")
            return True

        for attempt in range(1, self._max_retries + 1):
            logger.info("Starting database (attempt %d/%d), current status: %s",
                        attempt, self._max_retries, status)

            if status == "SHUTDOWN":
                rc, stdout, stderr = self._run_sqlplus("STARTUP;")
                if rc == 0 and "ORA-" not in stdout:
                    logger.info("Database startup initiated.")
                else:
                    logger.error("Startup failed: %s %s", stdout[:300], stderr[:300])
                    time.sleep(self._retry_delay)
                    status = self.check_db_status()
                    continue

            elif status == "MOUNTED":
                rc, stdout, stderr = self._run_sqlplus("ALTER DATABASE OPEN;")
                if rc == 0 and "ORA-" not in stdout:
                    logger.info("Database opened.")
                else:
                    logger.error("Open failed: %s %s", stdout[:300], stderr[:300])
                    time.sleep(self._retry_delay)
                    status = self.check_db_status()
                    continue

            elif status == "STARTED":
                # Mount first, then open
                rc, stdout, stderr = self._run_sqlplus(
                    "ALTER DATABASE MOUNT;\nALTER DATABASE OPEN;"
                )
                if rc == 0 and "ORA-" not in stdout:
                    logger.info("Database mounted and opened.")
                else:
                    logger.error("Mount/Open failed: %s %s", stdout[:300], stderr[:300])
                    time.sleep(self._retry_delay)
                    status = self.check_db_status()
                    continue

            # Verify final status
            time.sleep(5)
            final_status = self.check_db_status()
            if final_status == "OPEN":
                logger.info("Database is now OPEN.")
                return True

            status = final_status
            time.sleep(self._retry_delay)

        logger.error("Failed to start database after %d attempts.", self._max_retries)
        return False

    def start_listener(self) -> bool:
        """Start the Oracle listener if it's not running."""
        if self.check_listener_status():
            logger.info("Listener is already running.")
            return True

        logger.info("Starting listener...")
        rc, stdout, stderr = self._run_lsnrctl("start")
        if rc == 0:
            logger.info("Listener started successfully.")
            return True
        logger.error("Failed to start listener: %s %s", stdout[:200], stderr[:200])
        return False

    def auto_start(self) -> bool:
        """Full auto-start sequence: start DB, then listener.

        Returns True if both database and listener are running.
        """
        if not self._config.auto_start.enabled:
            logger.info("Auto-start is disabled in configuration.")
            return False

        actions: list[str] = []
        db_ok = False
        listener_ok = False

        # Start database
        initial_status = self.check_db_status()
        if initial_status != "OPEN":
            db_ok = self.start_database()
            if db_ok:
                actions.append(f"Database started (was {initial_status}, now OPEN)")
            else:
                actions.append(f"Database startup FAILED (status: {initial_status})")
        else:
            db_ok = True
            actions.append("Database was already OPEN")

        # Start listener
        if self._config.auto_start.start_listener:
            was_running = self.check_listener_status()
            if not was_running:
                listener_ok = self.start_listener()
                if listener_ok:
                    actions.append("Listener started")
                else:
                    actions.append("Listener startup FAILED")
            else:
                listener_ok = True
                actions.append("Listener was already running")

        # Send notification
        self._send_startup_alert(db_ok, listener_ok, actions)

        return db_ok and (listener_ok or not self._config.auto_start.start_listener)

    def _send_startup_alert(
        self, db_ok: bool, listener_ok: bool, actions: list[str]
    ) -> None:
        """Send email notification about startup results."""
        status_icon = "SUCCESS" if (db_ok and listener_ok) else "ATTENTION REQUIRED"
        actions_html = "<ul>" + "".join(f"<li>{a}</li>" for a in actions) + "</ul>"

        html = f"""
        <h3>Database Auto-Start Report: {status_icon}</h3>
        <table border="1" cellpadding="6" cellspacing="0">
            <tr><td><strong>Database</strong></td>
                <td style="color:{'green' if db_ok else 'red'};font-weight:bold">
                    {'OPEN' if db_ok else 'FAILED'}
                </td></tr>
            <tr><td><strong>Listener</strong></td>
                <td style="color:{'green' if listener_ok else 'red'};font-weight:bold">
                    {'RUNNING' if listener_ok else 'FAILED'}
                </td></tr>
            <tr><td><strong>SID</strong></td><td>{self._oracle_sid}</td></tr>
            <tr><td><strong>ORACLE_HOME</strong></td><td>{self._oracle_home}</td></tr>
        </table>
        <h4>Actions:</h4>
        {actions_html}
        """
        subject_prefix = "OK" if (db_ok and listener_ok) else "ALERT"
        self._alerter.send_alert(
            subject=f"{subject_prefix}: Auto-Start Report for {self._oracle_sid}",
            body_html=html,
        )

    def get_status_report(self) -> str:
        """Generate a text status report."""
        db_status = self.check_db_status()
        listener_ok = self.check_listener_status()
        lines = [
            "=" * 60,
            f"{'DATABASE STATUS REPORT':^60}",
            "=" * 60,
            f"  SID:          {self._oracle_sid}",
            f"  ORACLE_HOME:  {self._oracle_home}",
            f"  DB Status:    {db_status}",
            f"  Listener:     {'RUNNING' if listener_ok else 'STOPPED'}",
            "=" * 60,
        ]
        return "\n".join(lines)
