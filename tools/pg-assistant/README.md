# DB Assistant (pg-assistant)

AI-powered database assistant that converts natural language questions into SQL
queries using a local LLM (Ollama) and executes them against **PostgreSQL** or
**Oracle** databases via a Streamlit web UI.

## Architecture

```
┌──────────────────────────────────────────────────────┐
│                  Streamlit Web UI                     │
│  (app.py)                                            │
│  ┌──────────┬──────────┬───────────┬───────────────┐ │
│  │  Query   │  Schema  │  Auto     │  Auto         │ │
│  │  Tab     │  Tab     │  Monitor  │  Analyse      │ │
│  └──────────┴──────────┴───────────┴───────────────┘ │
└──────────┬──────────────┬──────────────┬─────────────┘
           │              │              │
    ┌──────▼──────┐ ┌─────▼─────┐ ┌─────▼──────┐
    │ sql_generator│ │ auto_     │ │ auto_      │
    │ .py         │ │ monitor.py│ │ analyse.py │
    └──────┬──────┘ └─────┬─────┘ └─────┬──────┘
           │              │              │
    ┌──────▼──────┐       │       ┌──────▼──────┐
    │ llm_client  │       │       │ llm_client  │
    │ .py (Ollama)│       │       │ .py (Ollama)│
    └─────────────┘       │       └─────────────┘
                          │
              ┌───────────▼───────────┐
              │     db_client.py      │
              │  ┌─────────┬────────┐ │
              │  │ Postgre │ Oracle │ │
              │  │ SQL     │ Client │ │
              │  │ Client  │        │ │
              │  └─────────┴────────┘ │
              └───────────────────────┘
              ┌───────────────────────┐
              │  profile_manager.py   │
              │  (~/.pg-assistant/    │
              │   profiles.json)      │
              └───────────────────────┘
```

| Module              | Purpose                                              |
|---------------------|------------------------------------------------------|
| `app.py`            | Streamlit web UI — tabs for Query, Schema, Monitor, Analyse, History |
| `db_client.py`      | Abstract DB client with PostgreSQL (psycopg2) and Oracle (oracledb) implementations |
| `llm_client.py`     | Ollama REST API client (`/api/generate`)              |
| `sql_generator.py`  | Prompt engineering, SQL extraction, safety validation, retry logic |
| `profile_manager.py`| Save/load/delete connection profiles as JSON          |
| `auto_monitor.py`   | Periodic tablespace monitoring, auto-extend datafiles (Oracle) |
| `auto_analyse.py`   | AWR/V$ (Oracle) and pg_stat_statements (PG) analysis with LLM summary |

## Prerequisites

- **Python 3.10+**
- **Ollama** running locally with a model (e.g. `codellama`)
- **PostgreSQL** and/or **Oracle** database accessible from this machine
- For Oracle: `oracledb` uses thin mode (no Oracle Client installation needed)

## Installation

```bash
cd tools/pg-assistant
pip install -r requirements.txt
```

### Dependencies

| Package           | Purpose                    |
|-------------------|----------------------------|
| `requests`        | Ollama HTTP API calls      |
| `psycopg2-binary` | PostgreSQL driver          |
| `oracledb`        | Oracle driver (thin mode)  |
| `streamlit`       | Web UI framework           |
| `pandas`          | DataFrame display & CSV    |

> You only need the driver for the database(s) you plan to connect to.
> If you only use PostgreSQL, `oracledb` is optional (and vice versa).

## Usage

```bash
streamlit run app.py
```

Then open **http://localhost:8501** in your browser.

### Web UI Features

1. **Sidebar** — Configure Ollama URL/model, select database type
   (PostgreSQL or Oracle), enter connection details, save/load/delete profiles.

2. **Query Tab** — Type a natural language question, see the generated SQL,
   review results in an interactive table, download as CSV.

3. **Schema Tab** — Browse database schema with expandable table views.

4. **Auto Monitor Tab** — Configure threshold, interval, and max file size.
   Start periodic monitoring or run a one-time check.
   - **Oracle**: Monitors tablespace usage via `DBA_DATA_FILES` / `DBA_FREE_SPACE`.
     Automatically enables autoextend or adds datafiles when usage exceeds threshold
     (max 20 GB per file by default).
   - **PostgreSQL**: Reports tablespace, database, and table sizes.

5. **Auto Analyse Tab** — Collect performance data and generate an AI-powered
   summary with action plan.
   - **Oracle**: Queries `V$SQL`, `V$SYSTEM_EVENT`, `V$SYSSTAT`, `V$SGAINFO`,
     `V$FILESTAT` for performance metrics.
   - **PostgreSQL**: Queries `pg_stat_statements`, `pg_stat_user_tables`,
     `pg_stat_database`, `pg_stat_bgwriter`, `pg_stat_user_indexes`.

6. **History Tab** — Review past queries with status, row counts, and timing.

## Connection Profiles

Profiles are stored in `~/.pg-assistant/profiles.json` and include:

| Field          | Description                     |
|----------------|---------------------------------|
| `db_type`      | `postgresql` or `oracle`        |
| `host`         | Database hostname               |
| `port`         | Database port                   |
| `database`     | Database name (PostgreSQL)      |
| `service_name` | Service name (Oracle)           |
| `user`         | Database username               |
| `password`     | Database password (plaintext)   |
| `sslmode`      | SSL mode (PostgreSQL only)      |

> **Security note**: Passwords are stored in plaintext. Use file-system
> permissions to restrict access, or consider integrating with a secrets
> manager for production use.

## SQL Safety

The SQL generator blocks dangerous keywords before execution:

`DROP`, `DELETE`, `TRUNCATE`, `UPDATE`, `INSERT`, `ALTER`, `CREATE`,
`GRANT`, `REVOKE`, `EXEC`, `EXECUTE`

Only `SELECT` and `WITH` (CTE) queries are allowed through the natural
language query path. The auto-monitor uses a separate internal path for
administrative DDL (e.g. `ALTER TABLESPACE`).

## Schema Awareness

On connection, the tool fetches schema metadata and injects it into every
LLM prompt so the model generates accurate, table-aware SQL.

- **PostgreSQL**: Queries `information_schema.tables` / `information_schema.columns`
- **Oracle**: Queries `ALL_TAB_COLUMNS`
