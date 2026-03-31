"""Configuration loader for Oracle DB Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class DatabaseConfig:
    host: str = "localhost"
    port: int = 1521
    service_name: str = "ORCL"
    username: str = "db_agent_user"
    password_env: str = "DB_AGENT_PASSWORD"
    is_cdb: bool = False
    pool_min: int = 1
    pool_max: int = 5
    pool_increment: int = 1

    @property
    def password(self) -> str:
        val = os.environ.get(self.password_env, "")
        if not val:
            raise OSError(f"Environment variable '{self.password_env}' is not set or empty.")
        return val

    @property
    def dsn(self) -> str:
        return f"{self.host}:{self.port}/{self.service_name}"


@dataclass
class OracleEnvConfig:
    oracle_home: str = "/u01/app/oracle/product/19.0.0/dbhome_1"
    oracle_sid: str = "ORCL"
    oracle_base: str = "/u01/app/oracle"


@dataclass
class ThresholdsConfig:
    tablespace_warning_pct: float = 80.0
    tablespace_critical_pct: float = 90.0
    temp_warning_pct: float = 75.0
    undo_warning_pct: float = 80.0
    long_query_seconds: int = 300
    session_utilization_warning_pct: float = 80.0
    fra_warning_pct: float = 80.0
    index_blevel_threshold: int = 3


@dataclass
class TablespaceManagementConfig:
    auto_extend: bool = True
    extend_size_mb: int = 512
    max_datafile_size_gb: int = 32
    datafile_path: str = "/u01/app/oracle/oradata/ORCL/"
    exclude_tablespaces: list[str] = field(default_factory=lambda: ["SYSTEM", "SYSAUX"])


@dataclass
class MaintenanceWindow:
    start: str = "22:00"
    end: str = "05:00"
    days: list[str] = field(
        default_factory=lambda: ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
    )


@dataclass
class StatsCollectionConfig:
    schemas: list[str] = field(default_factory=lambda: ["APP_SCHEMA"])
    exclude_tables: list[str] = field(default_factory=list)
    parallel_degree: int = 4
    estimate_percent: int = 0
    maintenance_window: MaintenanceWindow = field(default_factory=MaintenanceWindow)


@dataclass
class AutoStartConfig:
    enabled: bool = True
    start_listener: bool = True
    max_retries: int = 3
    retry_delay_seconds: int = 30


@dataclass
class PerformanceTestingConfig:
    pre_run_tasks: list[str] = field(default_factory=list)
    post_run_tasks: list[str] = field(default_factory=list)
    awr_report_dir: str = "/u01/app/oracle/admin/ORCL/awr_reports"


@dataclass
class EmailConfig:
    enabled: bool = True
    smtp_host: str = "smtp.example.com"
    smtp_port: int = 587
    smtp_use_tls: bool = True
    smtp_username_env: str = "SMTP_USERNAME"
    smtp_password_env: str = "SMTP_PASSWORD"
    from_address: str = "db-agent@example.com"
    recipients: list[str] = field(default_factory=list)

    @property
    def smtp_username(self) -> str:
        return os.environ.get(self.smtp_username_env, "")

    @property
    def smtp_password(self) -> str:
        return os.environ.get(self.smtp_password_env, "")


@dataclass
class AlertsConfig:
    email: EmailConfig = field(default_factory=EmailConfig)
    log_file: str = "/var/log/db_agent/agent.log"
    log_max_bytes: int = 10_485_760
    log_backup_count: int = 5


@dataclass
class ApprovalConfig:
    require_approval: bool = True
    approval_file: str = "/var/log/db_agent/pending_approvals.json"
    actions_requiring_approval: list[str] = field(
        default_factory=lambda: [
            "kill_session",
            "move_object",
            "drop_temp_tablespace",
            "rebuild_index",
            "delete_archivelogs",
        ]
    )


@dataclass
class AgentConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    oracle_env: OracleEnvConfig = field(default_factory=OracleEnvConfig)
    thresholds: ThresholdsConfig = field(default_factory=ThresholdsConfig)
    tablespace_management: TablespaceManagementConfig = field(default_factory=TablespaceManagementConfig)
    stats_collection: StatsCollectionConfig = field(default_factory=StatsCollectionConfig)
    auto_start: AutoStartConfig = field(default_factory=AutoStartConfig)
    performance_testing: PerformanceTestingConfig = field(default_factory=PerformanceTestingConfig)
    alerts: AlertsConfig = field(default_factory=AlertsConfig)
    approval: ApprovalConfig = field(default_factory=ApprovalConfig)
    system_schemas: list[str] = field(
        default_factory=lambda: [
            "SYS", "SYSTEM", "OUTLN", "DBSNMP", "APPQOSSYS", "WMSYS", "XDB",
            "ORDSYS", "ORDDATA", "CTXSYS", "MDSYS", "OLAPSYS", "LBACSYS",
            "DVSYS", "DBSFWUSER", "AUDSYS", "GSMADMIN_INTERNAL", "OJVMSYS",
        ]
    )


def _build_dataclass(cls: type, data: dict[str, Any] | None) -> Any:
    """Recursively build a dataclass from a dictionary."""
    if data is None:
        return cls()
    field_types = {f.name: f.type for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    kwargs: dict[str, Any] = {}
    for key, value in data.items():
        if key in field_types:
            ft = field_types[key]
            # Check if the field type is itself a dataclass
            if isinstance(ft, type) and hasattr(ft, "__dataclass_fields__"):
                kwargs[key] = _build_dataclass(ft, value)
            else:
                kwargs[key] = value
    return cls(**kwargs)


def load_config(config_path: str) -> AgentConfig:
    """Load and parse the YAML configuration file into an AgentConfig."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(path, encoding="utf-8") as f:
        raw: dict[str, Any] = yaml.safe_load(f) or {}

    config = AgentConfig()

    if "database" in raw:
        config.database = _build_dataclass(DatabaseConfig, raw["database"])
    if "oracle_env" in raw:
        config.oracle_env = _build_dataclass(OracleEnvConfig, raw["oracle_env"])
    if "thresholds" in raw:
        config.thresholds = _build_dataclass(ThresholdsConfig, raw["thresholds"])
    if "tablespace_management" in raw:
        config.tablespace_management = _build_dataclass(TablespaceManagementConfig, raw["tablespace_management"])
    if "stats_collection" in raw:
        sc_data = raw["stats_collection"]
        if "maintenance_window" in sc_data:
            sc_data["maintenance_window"] = _build_dataclass(MaintenanceWindow, sc_data["maintenance_window"])
        config.stats_collection = _build_dataclass(StatsCollectionConfig, sc_data)
    if "auto_start" in raw:
        config.auto_start = _build_dataclass(AutoStartConfig, raw["auto_start"])
    if "performance_testing" in raw:
        config.performance_testing = _build_dataclass(PerformanceTestingConfig, raw["performance_testing"])
    if "alerts" in raw:
        alerts_data = raw["alerts"]
        if "email" in alerts_data:
            alerts_data["email"] = _build_dataclass(EmailConfig, alerts_data["email"])
        config.alerts = _build_dataclass(AlertsConfig, alerts_data)
    if "approval" in raw:
        config.approval = _build_dataclass(ApprovalConfig, raw["approval"])
    if "system_schemas" in raw:
        config.system_schemas = raw["system_schemas"]

    return config
