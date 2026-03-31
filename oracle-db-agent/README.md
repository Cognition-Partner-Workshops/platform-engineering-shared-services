# Oracle DB Agent

Autonomous Oracle 19c Database Agent for routine DBA activities and performance testing support. Runs tasks **before and after** performance test runs (never during), ensuring zero interference with test metrics.

## Features

| Module | Description |
|--------|-------------|
| **Tablespace Manager** | Monitors usage, auto-extends/creates datafiles at configurable thresholds |
| **Object Auditor** | Detects non-system objects in SYSTEM/SYSAUX, generates move DDL |
| **Stats Collector** | Gathers stale statistics in user-defined maintenance windows |
| **Auto-Start** | Starts DB + listener after reboot (systemd or `@reboot` cron) |
| **AWR/ADDM Manager** | Creates snapshots, generates HTML AWR and text ADDM reports |
| **Temp Monitor** | Tracks temp tablespace usage, identifies heavy consumers |
| **Undo Monitor** | Monitors undo usage, detects ORA-01555 risk |
| **Session Monitor** | Finds long-running queries, blocking locks; approval-gated kill |
| **Alert Log Monitor** | Parses `V$DIAG_ALERT_EXT` for ORA- errors, categorizes by severity |
| **Invalid Objects** | Detects and recompiles invalid packages/views/triggers |
| **Resource Monitor** | Checks sessions/processes vs limits, SGA/PGA utilization |
| **Index Health** | Identifies fragmented (high blevel) or unusable indexes |

### Performance Testing Integration

```bash
# Before your test run
db-agent pre-run --force-stats

# After your test run
db-agent post-run
```

**Pre-run** checks DB status, gathers stats, recompiles invalid objects, takes AWR baseline, verifies resource headroom.

**Post-run** takes AWR end snapshot, generates AWR/ADDM reports, checks alert log, reports orphan sessions.

### Approval Workflow

Destructive actions (kill session, move objects, rebuild indexes, delete archivelogs) require explicit approval via email notification:

```bash
db-agent pending            # List pending approvals
db-agent approve <id>       # Approve an action
db-agent reject <id>        # Reject an action
```

## Requirements

- Python 3.9+
- Oracle 19c (traditional / non-CDB architecture)
- Filesystem-based storage
- `python-oracledb` (thin mode — no Oracle Client needed for remote connections)

## Installation

```bash
cd oracle-db-agent
pip install .

# Copy and edit the config
sudo mkdir -p /etc/oracle-db-agent
sudo cp config/db_agent_config.yaml /etc/oracle-db-agent/
sudo vi /etc/oracle-db-agent/db_agent_config.yaml

# Set required environment variables
export DB_AGENT_PASSWORD="your_db_password"
export SMTP_USERNAME="your_smtp_user"
export SMTP_PASSWORD="your_smtp_pass"
```

## Configuration

Edit `config/db_agent_config.yaml` to set:

- **Database connection** (host, port, service_name, credentials via env var)
- **Thresholds** (tablespace %, temp %, session limits, index blevel)
- **Stats collection** (schemas, time window, parallelism)
- **Performance testing** (pre-run/post-run task lists)
- **Email alerting** (SMTP settings, recipients)
- **Approval workflow** (which actions require approval)

## CLI Reference

```
db-agent [--config PATH] [--verbose] COMMAND

Commands:
  pre-run        Execute pre-performance-test checks
  post-run       Execute post-performance-test checks
  monitor        Run a full monitoring pass
  auto-start     Start DB/listener if down (post-reboot)
  report         Generate comprehensive health report
  gather-stats   Gather optimizer statistics
  approve ID     Approve a pending destructive action
  reject ID      Reject a pending destructive action
  pending        List pending approval requests

  check tablespace   Check tablespace usage
  check objects      Check non-system objects in SYSTEM/SYSAUX
  check stats        Check for stale statistics
  check temp         Check temp tablespace usage
  check undo         Check undo usage and ORA-01555 risk
  check sessions     Check long-running queries and blocking
  check resources    Check resource limits
  check indexes      Check index health
  check alertlog     Check alert log for errors
  check invalid      Check invalid database objects
  check status       Check DB and listener status

  awr snapshot       Create an AWR snapshot
  awr report         Generate AWR/ADDM reports
```

## Scheduling

### Cron (recommended)

```bash
# Install as oracle user
crontab -e
# Paste contents of crontab.example
```

### Systemd

```bash
sudo cp systemd/*.service systemd/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable oracle-db-agent-autostart.service
sudo systemctl enable oracle-db-agent-monitor.timer
sudo systemctl start oracle-db-agent-monitor.timer
```

## Database User Setup

Create a dedicated agent user with minimal privileges:

```sql
CREATE USER db_agent_user IDENTIFIED BY "secure_password";

GRANT CREATE SESSION TO db_agent_user;
GRANT SELECT ANY DICTIONARY TO db_agent_user;
GRANT ALTER TABLESPACE TO db_agent_user;
GRANT ALTER DATABASE TO db_agent_user;
GRANT EXECUTE ON DBMS_STATS TO db_agent_user;
GRANT EXECUTE ON DBMS_WORKLOAD_REPOSITORY TO db_agent_user;
GRANT EXECUTE ON DBMS_ADVISOR TO db_agent_user;
GRANT EXECUTE ON UTL_RECOMP TO db_agent_user;
GRANT ALTER SYSTEM TO db_agent_user;
GRANT ADVISOR TO db_agent_user;
```

## Security

- Passwords are **never stored in config files** — only environment variable names
- All actions are **logged with timestamps** for audit trail
- Destructive actions require **explicit approval** via CLI
- Supports Oracle Wallet as an alternative authentication method
