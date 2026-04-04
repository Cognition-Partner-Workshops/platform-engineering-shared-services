#!/usr/bin/env python3
"""AI-powered PostgreSQL assistant — Streamlit web UI.

Converts natural language questions into SQL queries using a local LLM (Ollama)
and executes them directly against a PostgreSQL database.
"""

import time

import pandas as pd
import streamlit as st

from db_client import DBClient
from llm_client import LLMClient
from profile_manager import ProfileManager
from sql_generator import SQLGenerationError, SQLGenerator, UnsafeSQLError

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="PG Assistant",
    page_icon="🐘",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Session-state defaults
# ---------------------------------------------------------------------------
_defaults: dict = {
    "db_client": None,
    "llm_client": None,
    "sql_generator": None,
    "schema_metadata": None,
    "query_history": [],
}
for _key, _val in _defaults.items():
    if _key not in st.session_state:
        st.session_state[_key] = _val

profile_mgr = ProfileManager()

# ---------------------------------------------------------------------------
# Sidebar — connection & profile management
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("🐘 PG Assistant")
    st.caption("AI-powered PostgreSQL query tool")
    st.divider()

    # --- Ollama settings ---------------------------------------------------
    st.subheader("🤖 Ollama Settings")
    ollama_url = st.text_input("Ollama URL", value="http://localhost:11434")
    ollama_model = st.text_input("Model", value="codellama")

    if st.button("Test Ollama Connection"):
        test_llm = LLMClient(base_url=ollama_url, model=ollama_model)
        if test_llm.health_check():
            models = test_llm.list_models()
            model_names = [m.get("name", "?") for m in (models or [])]
            st.success(f"Connected! Models: {', '.join(model_names)}")
        else:
            st.error(f"Cannot reach Ollama at {ollama_url}")

    st.divider()

    # --- Database connection ------------------------------------------------
    st.subheader("🗄️ Database Connection")

    saved_profiles = profile_mgr.list_profiles()
    profile_options = ["-- New Connection --"] + saved_profiles
    selected_profile = st.selectbox("Load Profile", profile_options)

    profile_data: dict = {}
    if selected_profile != "-- New Connection --":
        profile_data = profile_mgr.get_profile(selected_profile) or {}

    col1, col2 = st.columns(2)
    with col1:
        db_host = st.text_input("Host", value=profile_data.get("host", "localhost"))
        db_port = st.number_input(
            "Port",
            value=profile_data.get("port", 5432),
            min_value=1,
            max_value=65535,
            step=1,
        )
        db_name = st.text_input(
            "Database", value=profile_data.get("database", "postgres")
        )
    with col2:
        db_user = st.text_input("User", value=profile_data.get("user", "postgres"))
        db_password = st.text_input(
            "Password",
            value=profile_data.get("password", ""),
            type="password",
        )
        db_sslmode = st.selectbox(
            "SSL Mode",
            ["prefer", "disable", "require", "verify-ca", "verify-full"],
            index=[
                "prefer",
                "disable",
                "require",
                "verify-ca",
                "verify-full",
            ].index(profile_data.get("sslmode", "prefer")),
        )

    if st.button("🔌 Connect", use_container_width=True, type="primary"):
        try:
            db = DBClient(
                host=db_host,
                port=int(db_port),
                database=db_name,
                user=db_user,
                password=db_password,
                sslmode=db_sslmode,
            )
            db.connect()
            st.session_state.db_client = db

            llm = LLMClient(base_url=ollama_url, model=ollama_model)
            st.session_state.llm_client = llm
            gen = SQLGenerator(llm_client=llm)
            st.session_state.sql_generator = gen

            schema = db.get_schema()
            if schema:
                gen.update_schema(schema)
                st.session_state.schema_metadata = schema

            st.success(f"Connected to {db.get_connection_info()}")
        except ConnectionError as exc:
            st.error(str(exc))

    if st.session_state.db_client and st.session_state.db_client.is_connected:
        if st.button("Disconnect", use_container_width=True):
            st.session_state.db_client.disconnect()
            st.session_state.db_client = None
            st.session_state.sql_generator = None
            st.session_state.schema_metadata = None
            st.rerun()

    st.divider()

    # --- Profile save / delete ----------------------------------------------
    st.subheader("💾 Save Profile")
    profile_name = st.text_input("Profile Name", placeholder="e.g. production-db")
    if st.button("Save Current Settings", use_container_width=True):
        if not profile_name:
            st.warning("Enter a profile name first.")
        else:
            profile_mgr.save_profile(
                name=profile_name,
                host=db_host,
                port=int(db_port),
                database=db_name,
                user=db_user,
                password=db_password,
                sslmode=db_sslmode,
            )
            st.success(f"Profile '{profile_name}' saved!")
            st.rerun()

    if saved_profiles:
        st.divider()
        st.subheader("🗑️ Delete Profile")
        delete_target = st.selectbox(
            "Select profile", saved_profiles, key="del_profile"
        )
        if st.button("Delete", use_container_width=True):
            profile_mgr.delete_profile(delete_target)
            st.success(f"Profile '{delete_target}' deleted.")
            st.rerun()

# ---------------------------------------------------------------------------
# Main area
# ---------------------------------------------------------------------------
st.header("🐘 AI PostgreSQL Assistant")

if st.session_state.db_client and st.session_state.db_client.is_connected:
    st.info(
        f"Connected to **{st.session_state.db_client.get_connection_info()}** "
        f"| Model: **{ollama_model}**"
    )
else:
    st.warning("Not connected to a database. Use the sidebar to connect.")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
tab_query, tab_schema, tab_history = st.tabs(["💬 Query", "📋 Schema", "📜 History"])

# ---- Query tab ------------------------------------------------------------
with tab_query:
    st.subheader("Ask a question in natural language")

    user_question = st.text_area(
        "Your question",
        placeholder="e.g. Show me the top 10 largest tables by row count",
        height=100,
        label_visibility="collapsed",
    )

    col_run, col_examples = st.columns([1, 3])
    with col_run:
        run_btn = st.button(
            "🚀 Run Query",
            use_container_width=True,
            type="primary",
            disabled=not (
                st.session_state.db_client
                and st.session_state.db_client.is_connected
                and user_question.strip()
            ),
        )
    with col_examples:
        with st.expander("Example questions"):
            st.markdown(
                "- Show me all tables in the database\n"
                "- What are the top 10 largest tables by row count?\n"
                "- List all active connections to the database\n"
                "- Show the slowest queries from pg_stat_statements\n"
                "- What indexes exist on the users table?\n"
                "- Show database size for each table"
            )

    if run_btn and user_question.strip():
        generator = st.session_state.sql_generator
        db = st.session_state.db_client

        if not generator or not db:
            st.error("Connect to a database first.")
        else:
            with st.spinner("Generating SQL..."):
                gen_start = time.monotonic()
                try:
                    sql = generator.generate_sql(user_question.strip())
                    gen_elapsed = time.monotonic() - gen_start
                except UnsafeSQLError as exc:
                    st.error(f"**Safety Block:** {exc}")
                    sql = None
                    gen_elapsed = 0
                except SQLGenerationError as exc:
                    st.error(f"**Generation Error:** {exc}")
                    sql = None
                    gen_elapsed = 0

            if sql:
                st.subheader("Generated SQL")
                st.code(sql, language="sql")
                st.caption(f"Generated in {gen_elapsed:.2f}s")

                with st.spinner("Executing query..."):
                    result = db.execute_query(sql)

                if "error" in result:
                    st.error(f"**Query Error:** {result['error']}")
                    st.session_state.query_history.append(
                        {
                            "question": user_question.strip(),
                            "sql": sql,
                            "status": "error",
                            "error": result["error"],
                            "elapsed_ms": result.get("elapsed_ms", 0),
                        }
                    )
                else:
                    rows = result.get("rows", [])
                    row_count = result.get("row_count", 0)
                    elapsed_ms = result.get("elapsed_ms", 0)

                    st.subheader("Results")
                    if rows:
                        df = pd.DataFrame(rows)
                        st.dataframe(df, use_container_width=True)
                        st.caption(f"{row_count} row(s) returned in {elapsed_ms}ms")

                        csv = df.to_csv(index=False)
                        st.download_button(
                            "📥 Download CSV",
                            csv,
                            file_name="query_results.csv",
                            mime="text/csv",
                        )
                    else:
                        st.info("Query returned no results.")

                    st.session_state.query_history.append(
                        {
                            "question": user_question.strip(),
                            "sql": sql,
                            "status": "success",
                            "row_count": row_count,
                            "elapsed_ms": elapsed_ms,
                        }
                    )

# ---- Schema tab -----------------------------------------------------------
with tab_schema:
    st.subheader("Database Schema")

    if st.session_state.db_client and st.session_state.db_client.is_connected:
        if st.button("🔄 Refresh Schema"):
            schema = st.session_state.db_client.get_schema()
            if schema:
                if st.session_state.sql_generator:
                    st.session_state.sql_generator.update_schema(schema)
                st.session_state.schema_metadata = schema
                st.success("Schema refreshed!")
            else:
                st.warning("Could not load schema.")

        schema = st.session_state.schema_metadata
        if schema:
            st.caption(f"{len(schema)} table(s) found")
            for table_name, columns in schema.items():
                with st.expander(f"📋 {table_name} ({len(columns)} columns)"):
                    col_df = pd.DataFrame(columns)
                    st.dataframe(col_df, use_container_width=True, hide_index=True)
        else:
            st.info("No schema loaded. Click 'Refresh Schema' to load.")
    else:
        st.warning("Connect to a database first.")

# ---- History tab ----------------------------------------------------------
with tab_history:
    st.subheader("Query History")

    history = st.session_state.query_history
    if history:
        if st.button("🗑️ Clear History"):
            st.session_state.query_history = []
            st.rerun()

        for _i, entry in enumerate(reversed(history), 1):
            status_label = "[OK]" if entry["status"] == "success" else "[ERR]"
            with st.expander(f"{status_label} {entry['question'][:80]}"):
                st.code(entry["sql"], language="sql")
                if entry["status"] == "success":
                    st.caption(
                        f"{entry.get('row_count', 0)} rows | "
                        f"{entry.get('elapsed_ms', 0)}ms"
                    )
                else:
                    st.error(entry.get("error", "Unknown error"))
    else:
        st.info("No queries yet. Ask a question in the Query tab!")
