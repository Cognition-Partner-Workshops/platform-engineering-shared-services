# pg-assistant — AI-Powered PostgreSQL CLI

A production-ready Python CLI that converts natural language questions into SQL queries using a local LLM (Ollama) and executes them against PostgreSQL via an MCP server.

## Architecture

```
User Question (natural language)
        │
        ▼
┌──────────────────┐
│  sql_generator   │  ← Prompt engineering + safety validation
│                  │
│  ┌────────────┐  │
│  │ llm_client │──┼──→ Ollama API (codellama)
│  └────────────┘  │
└────────┬─────────┘
         │ validated SELECT query
         ▼
┌──────────────────┐
│   mcp_client     │──→ MCP PostgreSQL Server
└──────────────────┘
         │
         ▼
   Formatted Results (rich tables)
```

| Module            | Responsibility                                  |
|-------------------|--------------------------------------------------|
| `app.py`          | CLI loop, argument parsing, rich output          |
| `llm_client.py`   | Ollama API communication                        |
| `mcp_client.py`   | MCP PostgreSQL server communication             |
| `sql_generator.py` | Prompt engineering, SQL extraction, safety checks |

## Prerequisites

- **Python 3.10+**
- **Ollama** running locally with the `codellama` model pulled:
  ```bash
  ollama serve &
  ollama pull codellama
  ```
- **MCP PostgreSQL server** running on `http://localhost:3000`
- **PostgreSQL** with `pg_stat_statements` enabled

## Installation

```bash
cd tools/pg-assistant
pip install -r requirements.txt
```

## Usage

```bash
# Basic usage (defaults: Ollama on :11434, MCP on :3000)
python app.py

# Custom endpoints
python app.py --ollama-url http://localhost:11434 --mcp-url http://localhost:3000

# Use a different model
python app.py --model mistral

# Verbose/debug logging
python app.py -v

# Specify a PostgreSQL schema
python app.py --schema my_schema
```

### CLI Commands

| Command    | Description                          |
|------------|--------------------------------------|
| `help`     | Show available commands and examples |
| `schema`   | Refresh and display database schema  |
| `clear`    | Clear the terminal screen            |
| `exit`     | Quit the application                 |

### Example Session

```
pg-assistant> Show me the top 5 largest tables

┌─────────────────────────────────────────────────┐
│ Generated SQL                                   │
├─────────────────────────────────────────────────┤
│ SELECT schemaname, relname, n_live_tup           │
│ FROM pg_stat_user_tables                         │
│ ORDER BY n_live_tup DESC                         │
│ LIMIT 5;                                         │
└─────────────────────────────────────────────────┘

┌─────────────┬──────────┬────────────┐
│ schemaname  │ relname  │ n_live_tup │
├─────────────┼──────────┼────────────┤
│ public      │ orders   │ 1000000    │
│ public      │ users    │ 500000     │
│ ...         │ ...      │ ...        │
└─────────────┴──────────┴────────────┘
5 row(s) returned in 42ms
```

## SQL Safety

The assistant enforces **read-only access** by:

1. Blocking dangerous keywords: `DROP`, `DELETE`, `TRUNCATE`, `UPDATE`, `INSERT`, `ALTER`, `CREATE`, `GRANT`, `REVOKE`, `EXEC`, `EXECUTE`
2. Requiring queries to start with `SELECT` or `WITH` (CTEs)
3. Stripping string literals before keyword scanning to avoid false positives

## Schema Awareness

On startup, the assistant fetches `information_schema` metadata and injects it into every LLM prompt. This provides the model with table names, column names, data types, and constraints — significantly improving SQL generation accuracy.

Refresh the schema at any time with the `schema` command.
