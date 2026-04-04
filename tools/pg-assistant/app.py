#!/usr/bin/env python3
"""AI-powered PostgreSQL assistant CLI application.

Converts natural language questions into SQL queries using a local LLM (Ollama)
and executes them against a PostgreSQL database via an MCP server.
"""

import argparse
import logging
import sys
import time

from rich.console import Console
from rich.logging import RichHandler
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llm_client import LLMClient
from mcp_client import MCPClient
from sql_generator import SQLGenerationError, SQLGenerator, UnsafeSQLError

console = Console()

HELP_TEXT = """
[bold cyan]Available Commands:[/bold cyan]

  [green]exit[/green] / [green]quit[/green]       Quit the application
  [green]help[/green]              Show this help message
  [green]schema[/green]            Refresh and display the database schema
  [green]clear[/green]             Clear the terminal screen

[bold cyan]Example Questions:[/bold cyan]

  • Show me all tables in the database
  • What are the top 10 largest tables by row count?
  • List all active connections to the database
  • Show the slowest queries from pg_stat_statements
  • What indexes exist on the users table?
  • Show me the table structure for the orders table
"""

BANNER = r"""
[bold cyan]╔══════════════════════════════════════════════════╗
║        AI PostgreSQL Assistant (pg-assistant)     ║
║  Natural Language → SQL via Ollama + MCP Server   ║
╚══════════════════════════════════════════════════╝[/bold cyan]
"""


def setup_logging(verbose: bool = False) -> None:
    """Configure logging with rich handler."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=False)],
    )


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="AI-powered PostgreSQL assistant using Ollama and MCP",
    )
    parser.add_argument(
        "--ollama-url",
        default="http://localhost:11434",
        help="Ollama server URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--mcp-url",
        default="http://localhost:3000",
        help="MCP PostgreSQL server URL (default: http://localhost:3000)",
    )
    parser.add_argument(
        "--model",
        default="codellama",
        help="Ollama model name (default: codellama)",
    )
    parser.add_argument(
        "--schema",
        default="public",
        help="PostgreSQL schema to use for context (default: public)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    return parser.parse_args()


def check_services(llm_client: LLMClient, mcp_client: MCPClient) -> bool:
    """Verify that Ollama and MCP services are reachable."""
    all_ok = True

    with console.status("[bold yellow]Checking Ollama server..."):
        if llm_client.health_check():
            console.print("  [green]✓[/green] Ollama server is reachable")
            models = llm_client.list_models()
            if models:
                model_names = [m.get("name", "unknown") for m in models]
                console.print(f"    Available models: {', '.join(model_names)}")
        else:
            console.print(
                f"  [red]✗[/red] Cannot reach Ollama at {llm_client.base_url}"
            )
            all_ok = False

    with console.status("[bold yellow]Checking MCP server..."):
        if mcp_client.health_check():
            console.print("  [green]✓[/green] MCP PostgreSQL server is reachable")
        else:
            console.print(
                f"  [red]✗[/red] Cannot reach MCP server at {mcp_client.base_url}"
            )
            all_ok = False

    return all_ok


def load_schema(
    mcp_client: MCPClient,
    sql_generator: SQLGenerator,
    schema_name: str,
) -> None:
    """Load and display database schema metadata."""
    with console.status("[bold yellow]Loading database schema..."):
        schema = mcp_client.get_schema(schema_name)

    if schema:
        sql_generator.update_schema(schema)
        display_schema(schema)
    else:
        console.print(
            "[yellow]⚠ Could not load schema metadata. "
            "SQL generation will proceed without schema context.[/yellow]"
        )


def display_schema(schema: dict) -> None:
    """Render the database schema as a rich table."""
    table = Table(
        title="Database Schema",
        show_header=True,
        header_style="bold magenta",
    )
    table.add_column("Table", style="cyan", no_wrap=True)
    table.add_column("Column", style="green")
    table.add_column("Type", style="yellow")
    table.add_column("Nullable", style="dim")

    for table_name, columns in schema.items():
        for i, col in enumerate(columns):
            table.add_row(
                table_name if i == 0 else "",
                col["column_name"],
                col["data_type"],
                col["is_nullable"],
            )
        table.add_section()

    console.print(table)


def display_results(result: dict) -> None:
    """Render query results as a rich table."""
    if "error" in result:
        console.print(f"\n[red]Query Error:[/red] {result['error']}")
        return

    columns = result.get("columns", [])
    rows = result.get("rows", [])
    row_count = result.get("row_count", len(rows))
    elapsed_ms = result.get("elapsed_ms", 0)

    if not rows:
        console.print("\n[yellow]Query returned no results.[/yellow]")
        return

    table = Table(
        title="Query Results",
        show_header=True,
        header_style="bold magenta",
        show_lines=True,
    )

    # Determine column names
    if columns:
        col_names = columns
    elif rows and isinstance(rows[0], dict):
        col_names = list(rows[0].keys())
    else:
        col_names = [f"col_{i}" for i in range(len(rows[0]) if rows else 0)]

    for col_name in col_names:
        table.add_column(str(col_name), style="cyan", overflow="fold")

    for row in rows:
        if isinstance(row, dict):
            table.add_row(*[str(v) if v is not None else "NULL" for v in row.values()])
        elif isinstance(row, (list, tuple)):
            table.add_row(*[str(v) if v is not None else "NULL" for v in row])

    console.print(table)
    console.print(f"\n[dim]{row_count} row(s) returned in {elapsed_ms}ms[/dim]")


def process_query(
    user_input: str,
    sql_generator: SQLGenerator,
    mcp_client: MCPClient,
) -> None:
    """Process a natural language query end-to-end."""
    # Step 1: Generate SQL
    console.print()
    with console.status("[bold yellow]Generating SQL..."):
        start_gen = time.monotonic()
        try:
            sql = sql_generator.generate_sql(user_input)
        except UnsafeSQLError as exc:
            console.print(f"\n[red]Safety Block:[/red] {exc}")
            return
        except SQLGenerationError as exc:
            console.print(f"\n[red]Generation Error:[/red] {exc}")
            return
        gen_elapsed = time.monotonic() - start_gen

    # Step 2: Display generated SQL
    console.print(
        Panel(
            Text(sql, style="green"),
            title="[bold]Generated SQL[/bold]",
            subtitle=f"[dim]generated in {gen_elapsed:.2f}s[/dim]",
            border_style="blue",
        )
    )

    # Step 3: Execute SQL
    with console.status("[bold yellow]Executing query..."):
        start_exec = time.monotonic()
        try:
            result = mcp_client.execute_query(sql)
        except (ConnectionError, RuntimeError) as exc:
            console.print(f"\n[red]Execution Error:[/red] {exc}")
            return
        exec_elapsed = time.monotonic() - start_exec

    # Step 4: Display results
    if "elapsed_ms" not in result:
        result["elapsed_ms"] = round(exec_elapsed * 1000, 2)

    display_results(result)


def main() -> None:
    """Main CLI entry point."""
    args = parse_args()
    setup_logging(verbose=args.verbose)

    console.print(BANNER)

    # Initialize clients
    llm_client = LLMClient(
        base_url=args.ollama_url,
        model=args.model,
    )
    mcp_client = MCPClient(base_url=args.mcp_url)
    sql_generator = SQLGenerator(llm_client=llm_client)

    # Check service connectivity
    if not check_services(llm_client, mcp_client):
        console.print(
            "\n[bold red]Some services are not available. "
            "Please ensure Ollama and MCP server are running.[/bold red]"
        )
        console.print(
            "[dim]Continuing anyway — errors will appear when you submit queries.[/dim]"
        )

    # Load schema
    load_schema(mcp_client, sql_generator, args.schema)

    console.print(
        '\n[dim]Type a natural language question, or "help" for commands.[/dim]\n'
    )

    # Main REPL loop
    while True:
        try:
            user_input = console.input(
                "[bold green]pg-assistant>[/bold green] "
            ).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Goodbye![/dim]")
            sys.exit(0)

        if not user_input:
            continue

        command = user_input.lower()

        if command in ("exit", "quit"):
            console.print("[dim]Goodbye![/dim]")
            sys.exit(0)

        if command == "help":
            console.print(HELP_TEXT)
            continue

        if command == "schema":
            load_schema(mcp_client, sql_generator, args.schema)
            continue

        if command == "clear":
            console.clear()
            continue

        process_query(user_input, sql_generator, mcp_client)


if __name__ == "__main__":
    main()
