"""CLI entrypoint for Oracle DB Agent."""

from __future__ import annotations

import sys

import click

from oracle_db_agent.config import load_config
from oracle_db_agent.logger import setup_logging
from oracle_db_agent.orchestrator import AgentOrchestrator

DEFAULT_CONFIG = "/etc/oracle-db-agent/db_agent_config.yaml"


def _get_orchestrator(config_path: str, verbose: bool) -> AgentOrchestrator:
    """Load config, set up logging, and return an initialized orchestrator."""
    config = load_config(config_path)
    setup_logging(config, verbose=verbose)
    return AgentOrchestrator(config)


# ---------------------------------------------------------------------------
# Main CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.option(
    "-c", "--config",
    default=DEFAULT_CONFIG,
    envvar="DB_AGENT_CONFIG",
    help="Path to configuration YAML file.",
    show_default=True,
)
@click.option("-v", "--verbose", is_flag=True, help="Enable debug logging.")
@click.pass_context
def main(ctx: click.Context, config: str, verbose: bool) -> None:
    """Oracle DB Agent - Autonomous database management for Oracle 19c.

    Run routine DBA tasks before/after performance tests, or on a schedule.
    """
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config
    ctx.obj["verbose"] = verbose


# ---------------------------------------------------------------------------
# Pre-run / Post-run commands
# ---------------------------------------------------------------------------


@main.command()
@click.option("--force-stats", is_flag=True, help="Force stats collection even outside maintenance window.")
@click.pass_context
def pre_run(ctx: click.Context, force_stats: bool) -> None:
    """Execute pre-performance-test checks and preparations.

    Runs the configured pre_run_tasks (tablespace check, stats gathering,
    invalid object recompilation, AWR snapshot, etc.).
    """
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        click.echo("ERROR: Failed to connect to database.", err=True)
        sys.exit(1)
    try:
        results = agent.run_pre_run(force_stats=force_stats)
        _print_results("Pre-Run", results)
        errors = [r for r in results.values() if r.startswith("ERROR") or r.startswith("CRITICAL")]
        if errors:
            sys.exit(1)
    finally:
        agent.shutdown()


@main.command()
@click.pass_context
def post_run(ctx: click.Context) -> None:
    """Execute post-performance-test checks and reporting.

    Runs the configured post_run_tasks (AWR report generation, alert log
    check, session cleanup, etc.).
    """
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        click.echo("ERROR: Failed to connect to database.", err=True)
        sys.exit(1)
    try:
        results = agent.run_post_run()
        _print_results("Post-Run", results)
    finally:
        agent.shutdown()


# ---------------------------------------------------------------------------
# Monitoring command
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def monitor(ctx: click.Context) -> None:
    """Run a full monitoring pass (all health checks).

    Checks tablespaces, resource limits, indexes, undo, temp, sessions,
    alert log, and non-system objects.
    """
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        click.echo("ERROR: Failed to connect to database.", err=True)
        sys.exit(1)
    try:
        results = agent.run_monitor()
        _print_results("Monitor", results)
    finally:
        agent.shutdown()


# ---------------------------------------------------------------------------
# Auto-start command
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def auto_start(ctx: click.Context) -> None:
    """Start database and listener if they are down (post-reboot).

    Checks instance status, starts the database if shutdown, and starts
    the listener. Sends an email notification with results.
    """
    config = load_config(ctx.obj["config_path"])
    setup_logging(config, verbose=ctx.obj["verbose"])
    agent = AgentOrchestrator(config)
    success = agent.run_auto_start()
    if success:
        click.echo("Database and listener are running.")
    else:
        click.echo("WARNING: Auto-start encountered issues. Check logs.", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Report command
# ---------------------------------------------------------------------------


@main.command()
@click.pass_context
def report(ctx: click.Context) -> None:
    """Generate a comprehensive database health report."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        click.echo("ERROR: Failed to connect to database.", err=True)
        sys.exit(1)
    try:
        full_report = agent.generate_full_report()
        click.echo(full_report)
    finally:
        agent.shutdown()


# ---------------------------------------------------------------------------
# Individual module commands
# ---------------------------------------------------------------------------


@main.group()
def check() -> None:
    """Run individual health checks."""


@check.command("tablespace")
@click.pass_context
def check_tablespace(ctx: click.Context) -> None:
    """Check tablespace usage and auto-manage if needed."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.tablespace.get_report())
    finally:
        agent.shutdown()


@check.command("objects")
@click.pass_context
def check_objects(ctx: click.Context) -> None:
    """Check for non-system objects in SYSTEM/SYSAUX tablespaces."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.object_auditor.get_report())
    finally:
        agent.shutdown()


@check.command("stats")
@click.pass_context
def check_stats(ctx: click.Context) -> None:
    """Check for tables with stale or missing statistics."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.stats_collector.get_report())
    finally:
        agent.shutdown()


@check.command("temp")
@click.pass_context
def check_temp(ctx: click.Context) -> None:
    """Check temp tablespace usage."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.temp.get_report())
    finally:
        agent.shutdown()


@check.command("undo")
@click.pass_context
def check_undo(ctx: click.Context) -> None:
    """Check undo tablespace usage and ORA-01555 risk."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.undo.get_report())
    finally:
        agent.shutdown()


@check.command("sessions")
@click.pass_context
def check_sessions(ctx: click.Context) -> None:
    """Check for long-running queries and blocking sessions."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.session.get_report())
    finally:
        agent.shutdown()


@check.command("resources")
@click.pass_context
def check_resources(ctx: click.Context) -> None:
    """Check resource limits (sessions, processes, etc.)."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.resource_monitor.get_report())
    finally:
        agent.shutdown()


@check.command("indexes")
@click.pass_context
def check_indexes(ctx: click.Context) -> None:
    """Check index health and provide rebuild recommendations."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.index_health.get_report())
    finally:
        agent.shutdown()


@check.command("alertlog")
@click.option("--hours", default=24, help="Number of hours to look back.")
@click.pass_context
def check_alertlog(ctx: click.Context, hours: int) -> None:
    """Check alert log for ORA- errors."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.alert_log.get_report(hours=hours))
    finally:
        agent.shutdown()


@check.command("invalid")
@click.pass_context
def check_invalid(ctx: click.Context) -> None:
    """Check for invalid database objects."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        click.echo(agent.invalid_objects.get_report())
    finally:
        agent.shutdown()


@check.command("status")
@click.pass_context
def check_status(ctx: click.Context) -> None:
    """Check database and listener status."""
    config = load_config(ctx.obj["config_path"])
    setup_logging(config, verbose=ctx.obj["verbose"])
    agent = AgentOrchestrator(config)
    click.echo(agent.auto_start_manager.get_status_report())


# ---------------------------------------------------------------------------
# Stats collection command
# ---------------------------------------------------------------------------


@main.command()
@click.option("--force", is_flag=True, help="Force stats collection outside maintenance window.")
@click.option("--schema", multiple=True, help="Specific schema(s) to gather stats for.")
@click.pass_context
def gather_stats(ctx: click.Context, force: bool, schema: tuple[str, ...]) -> None:
    """Gather optimizer statistics for configured schemas."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        if schema:
            for s in schema:
                success = agent.stats_collector.gather_schema_stats(s)
                click.echo(f"{'OK' if success else 'FAILED'}: {s}")
        else:
            count = agent.stats_collector.gather_stale_stats(force=force)
            click.echo(f"Stats gathered for {count} schema(s).")
    finally:
        agent.shutdown()


# ---------------------------------------------------------------------------
# AWR commands
# ---------------------------------------------------------------------------


@main.group()
def awr() -> None:
    """AWR snapshot and report management."""


@awr.command("snapshot")
@click.pass_context
def awr_snapshot(ctx: click.Context) -> None:
    """Create an AWR snapshot."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        snap_id = agent.awr.create_snapshot()
        if snap_id:
            click.echo(f"AWR snapshot created: snap_id={snap_id}")
        else:
            click.echo("Failed to create AWR snapshot.", err=True)
            sys.exit(1)
    finally:
        agent.shutdown()


@awr.command("report")
@click.option("--begin-snap", required=True, type=int, help="Beginning snapshot ID.")
@click.option("--end-snap", required=True, type=int, help="Ending snapshot ID.")
@click.option("--output", default=None, help="Output file path (default: auto-generated).")
@click.pass_context
def awr_report(ctx: click.Context, begin_snap: int, end_snap: int, output: str | None) -> None:
    """Generate AWR and ADDM reports between two snapshots."""
    agent = _get_orchestrator(ctx.obj["config_path"], ctx.obj["verbose"])
    if not agent.initialize():
        sys.exit(1)
    try:
        awr_path, addm_path = agent.awr.generate_reports_for_window(begin_snap, end_snap)
        if awr_path:
            click.echo(f"AWR report: {awr_path}")
        if addm_path:
            click.echo(f"ADDM report: {addm_path}")
        if not awr_path and not addm_path:
            click.echo("Failed to generate reports.", err=True)
            sys.exit(1)
    finally:
        agent.shutdown()


# ---------------------------------------------------------------------------
# Approval commands
# ---------------------------------------------------------------------------


@main.command()
@click.argument("approval_id")
@click.pass_context
def approve(ctx: click.Context, approval_id: str) -> None:
    """Approve a pending destructive action."""
    config = load_config(ctx.obj["config_path"])
    setup_logging(config, verbose=ctx.obj["verbose"])
    agent = AgentOrchestrator(config)
    if agent.approval_manager.approve(approval_id):
        click.echo(f"Approved: {approval_id}")
    else:
        click.echo(f"Approval ID not found: {approval_id}", err=True)
        sys.exit(1)


@main.command()
@click.argument("approval_id")
@click.pass_context
def reject(ctx: click.Context, approval_id: str) -> None:
    """Reject a pending destructive action."""
    config = load_config(ctx.obj["config_path"])
    setup_logging(config, verbose=ctx.obj["verbose"])
    agent = AgentOrchestrator(config)
    if agent.approval_manager.reject(approval_id):
        click.echo(f"Rejected: {approval_id}")
    else:
        click.echo(f"Approval ID not found: {approval_id}", err=True)
        sys.exit(1)


@main.command("pending")
@click.pass_context
def list_pending(ctx: click.Context) -> None:
    """List all pending approval requests."""
    config = load_config(ctx.obj["config_path"])
    setup_logging(config, verbose=ctx.obj["verbose"])
    agent = AgentOrchestrator(config)
    pending = agent.approval_manager.get_pending()
    if not pending:
        click.echo("No pending approvals.")
        return
    click.echo(f"{'ID':<10} {'Action':<20} {'Description':<40} {'Created':<25}")
    click.echo("-" * 95)
    for p in pending:
        click.echo(f"{p.approval_id:<10} {p.action:<20} {p.description:<40} {p.created_at:<25}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_results(title: str, results: dict[str, str]) -> None:
    """Print task results to console."""
    click.echo(f"\n{'=' * 70}")
    click.echo(f"  {title} Results")
    click.echo(f"{'=' * 70}")
    for task, result in results.items():
        icon = "  "
        if result.startswith("OK"):
            icon = "  "
        elif result.startswith("WARNING"):
            icon = "! "
        elif result.startswith("ERROR") or result.startswith("CRITICAL"):
            icon = "X "
        elif result.startswith("SKIPPED"):
            icon = "- "
        click.echo(f"  {icon}{task:<35} {result}")
    click.echo(f"{'=' * 70}\n")


if __name__ == "__main__":
    main()
