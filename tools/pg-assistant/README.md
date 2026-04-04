# pg-assistant — AI-Powered PostgreSQL Assistant

A Streamlit web UI that converts natural language questions into SQL queries using a local LLM (Ollama) and executes them directly against PostgreSQL. Includes connection profile management for saving and loading database configurations.

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
│    db_client     │──→ PostgreSQL (direct via psycopg2)
└──────────────────┘
         │
         ▼
   Streamlit Web UI (tables, charts, CSV export)
```

| Module              | Responsibility                                    |
|---------------------|---------------------------------------------------|
| `app.py`            | Streamlit web UI                                  |
| `llm_client.py`     | Ollama API communication                          |
| `db_client.py`      | Direct PostgreSQL connection via psycopg2          |
| `sql_generator.py`  | Prompt engineering, SQL extraction, safety checks  |
| `profile_manager.py`| Save / load database connection profiles (JSON)    |

## Prerequisites

- **Python 3.10+**
- **Ollama** running locally with the `codellama` model pulled:
  ```bash
  ollama serve &
  ollama pull codellama
  ```
- **PostgreSQL** database accessible from the machine running pg-assistant

## Installation

```bash
cd tools/pg-assistant
pip install -r requirements.txt
```

## Usage

```bash
# Start the Streamlit web UI
streamlit run app.py

# Or with a custom port
streamlit run app.py --server.port 8502
```

Then open the URL shown in your terminal (default: `http://localhost:8501`).

### Web UI Features

| Feature                | Description                                        |
|------------------------|----------------------------------------------------|
| **Ollama Settings**    | Configure Ollama URL and model in the sidebar       |
| **DB Connection**      | Enter host, port, database, user, password, SSL     |
| **Connection Profiles**| Save, load, and delete database connection profiles |
| **Query Tab**          | Type natural language questions, view generated SQL  |
| **Schema Tab**         | Browse database tables and columns                  |
| **History Tab**        | Review past queries and results                     |
| **CSV Export**         | Download query results as CSV                       |

### Connection Profiles

Profiles are saved to `~/.pg-assistant/profiles.json`. Each profile stores:
- Host, port, database name
- Username and password
- SSL mode

To use profiles:
1. Fill in connection details in the sidebar
2. Enter a profile name and click **Save Current Settings**
3. Next time, select the profile from the **Load Profile** dropdown
4. Click **Connect** to establish the connection

## SQL Safety

The assistant enforces **read-only access** by:

1. Blocking dangerous keywords: `DROP`, `DELETE`, `TRUNCATE`, `UPDATE`, `INSERT`, `ALTER`, `CREATE`, `GRANT`, `REVOKE`, `EXEC`, `EXECUTE`
2. Requiring queries to start with `SELECT` or `WITH` (CTEs)
3. Stripping string literals before keyword scanning to avoid false positives

## Schema Awareness

On connection, the assistant fetches `information_schema` metadata and injects it into every LLM prompt. This provides the model with table names, column names, data types, and constraints — significantly improving SQL generation accuracy.

Refresh the schema at any time via the **Schema** tab.
