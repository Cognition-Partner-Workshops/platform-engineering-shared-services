#!/usr/bin/env python3
"""AI-powered database assistant -- Streamlit web UI.

Converts natural language questions into SQL queries using a local LLM (Ollama)
and executes them directly against PostgreSQL or Oracle databases.
"""

import time

import pandas as pd
import streamlit as st

from auto_analyse import PerformanceAnalyser
from auto_monitor import TablespaceMonitor
from db_client import (
    DB_TYPE_ORACLE,
    DB_TYPE_POSTGRESQL,
    BaseDBClient,
    create_db_client,
)
from llm_client import LLMClient
from profile_manager import ProfileManager
from session_monitor import SessionMonitor
from sql_generator import SQLGenerationError, SQLGenerator, UnsafeSQLError
from sql_tuning_advisor import SQLTuningAdvisor

# ---------------------------------------------------------------------------
# Page config
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="DB Assistant",
    page_icon="🛢️",
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
    "monitor": None,
    "analyser": None,
}
for _key, _val in _defaults.items():
    if _key not in st.session_state:
        st.session_state[_key] = _val

profile_mgr = ProfileManager()

# ---------------------------------------------------------------------------
# Helper: current db_type from connected client
# ---------------------------------------------------------------------------


def _connected_db_type() -> str:
    client: BaseDBClient | None = st.session_state.db_client
    if client and client.is_connected:
        return client.db_type
    return ""


# ---------------------------------------------------------------------------
# Sidebar -- connection & profile management
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("🛢️ DB Assistant")
    st.caption("AI-powered PostgreSQL & Oracle query tool")
    st.divider()

    # --- Ollama settings ---------------------------------------------------
    st.subheader("🤖 Ollama Settings")
    ollama_url = st.text_input("Ollama URL", value="http://localhost:11434")
    ollama_model = st.text_input("Model", value="codellama")
    ollama_timeout = st.slider(
        "Request timeout (seconds)", 60, 600, 300, step=30, key="ollama_timeout"
    )

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

    db_type_options = ["PostgreSQL", "Oracle"]
    db_type_map = {"PostgreSQL": DB_TYPE_POSTGRESQL, "Oracle": DB_TYPE_ORACLE}
    reverse_map = {v: k for k, v in db_type_map.items()}

    saved_profiles = profile_mgr.list_profiles()
    profile_options = ["-- New Connection --"] + saved_profiles
    selected_profile = st.selectbox("Load Profile", profile_options)

    profile_data: dict = {}
    if selected_profile != "-- New Connection --":
        profile_data = profile_mgr.get_profile(selected_profile) or {}

    profile_db_type = profile_data.get("db_type", DB_TYPE_POSTGRESQL)
    default_type_idx = db_type_options.index(
        reverse_map.get(profile_db_type, "PostgreSQL")
    )
    selected_db_label = st.selectbox(
        "Database Type", db_type_options, index=default_type_idx
    )
    selected_db_type = db_type_map[selected_db_label]

    col1, col2 = st.columns(2)
    with col1:
        db_host = st.text_input("Host", value=profile_data.get("host", "localhost"))
        db_port = st.number_input(
            "Port",
            value=profile_data.get(
                "port", 5432 if selected_db_type == DB_TYPE_POSTGRESQL else 1521
            ),
            min_value=1,
            max_value=65535,
            step=1,
        )
        if selected_db_type == DB_TYPE_POSTGRESQL:
            db_name = st.text_input(
                "Database", value=profile_data.get("database", "postgres")
            )
        else:
            db_service = st.text_input(
                "Service Name", value=profile_data.get("service_name", "ORCL")
            )
    with col2:
        db_user = st.text_input(
            "User",
            value=profile_data.get(
                "user", "postgres" if selected_db_type == DB_TYPE_POSTGRESQL else ""
            ),
        )
        db_password = st.text_input(
            "Password",
            value=profile_data.get("password", ""),
            type="password",
        )
        if selected_db_type == DB_TYPE_POSTGRESQL:
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
            conn_kwargs: dict = {
                "host": db_host,
                "port": int(db_port),
                "user": db_user,
                "password": db_password,
            }
            if selected_db_type == DB_TYPE_POSTGRESQL:
                conn_kwargs["database"] = db_name
                conn_kwargs["sslmode"] = db_sslmode
            else:
                conn_kwargs["service_name"] = db_service

            db = create_db_client(selected_db_type, **conn_kwargs)
            db.connect()
            st.session_state.db_client = db

            llm = LLMClient(
                base_url=ollama_url, model=ollama_model, timeout=ollama_timeout
            )
            st.session_state.llm_client = llm
            gen = SQLGenerator(llm_client=llm, db_type=selected_db_type)
            st.session_state.sql_generator = gen

            schema = db.get_schema()
            if schema:
                gen.update_schema(schema)
                st.session_state.schema_metadata = schema

            st.session_state.monitor = None
            st.session_state.analyser = None

            st.success(f"Connected to {db.get_connection_info()}")
        except (ConnectionError, ImportError) as exc:
            st.error(str(exc))

    if st.session_state.db_client and st.session_state.db_client.is_connected:
        if st.button("Disconnect", use_container_width=True):
            if st.session_state.monitor:
                st.session_state.monitor.stop()
            st.session_state.db_client.disconnect()
            st.session_state.db_client = None
            st.session_state.sql_generator = None
            st.session_state.schema_metadata = None
            st.session_state.monitor = None
            st.session_state.analyser = None
            st.rerun()

    st.divider()

    # --- Profile save / delete ----------------------------------------------
    st.subheader("💾 Save Profile")
    profile_name = st.text_input("Profile Name", placeholder="e.g. production-db")
    if st.button("Save Current Settings", use_container_width=True):
        if not profile_name:
            st.warning("Enter a profile name first.")
        else:
            save_kwargs: dict = {
                "name": profile_name,
                "db_type": selected_db_type,
                "host": db_host,
                "port": int(db_port),
                "user": db_user,
                "password": db_password,
            }
            if selected_db_type == DB_TYPE_POSTGRESQL:
                save_kwargs["database"] = db_name
                save_kwargs["sslmode"] = db_sslmode
            else:
                save_kwargs["service_name"] = db_service
            profile_mgr.save_profile(**save_kwargs)
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
st.header("🛢️ AI Database Assistant")

if st.session_state.db_client and st.session_state.db_client.is_connected:
    db_label = _connected_db_type().upper()
    st.info(
        f"Connected to **{st.session_state.db_client.get_connection_info()}** "
        f"({db_label}) | Model: **{ollama_model}**"
    )
else:
    st.warning("Not connected to a database. Use the sidebar to connect.")

# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------
(
    tab_query,
    tab_schema,
    tab_monitor,
    tab_analyse,
    tab_sessions,
    tab_tuning,
    tab_history,
) = st.tabs(
    [
        "💬 Query",
        "📋 Schema",
        "📡 Auto Monitor",
        "📊 Auto Analyse",
        "🔒 Sessions & Locks",
        "🔧 SQL Tuning Advisor",
        "📜 History",
    ]
)

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
                max_exec_retries = 2
                for exec_attempt in range(1, max_exec_retries + 1):
                    st.subheader("Generated SQL")
                    st.code(sql, language="sql")
                    st.caption(f"Generated in {gen_elapsed:.2f}s")

                    with st.spinner("Executing query..."):
                        result = db.execute_query(sql)

                    if "error" in result and exec_attempt < max_exec_retries:
                        db_error = result["error"]
                        st.warning(
                            f"**Query failed** (attempt {exec_attempt}): {db_error}\n\n"
                            "Regenerating SQL with error feedback..."
                        )
                        with st.spinner("Regenerating SQL with error context..."):
                            retry_start = time.monotonic()
                            try:
                                sql = generator.generate_sql(
                                    f"{user_question.strip()}\n\n"
                                    f"IMPORTANT: The previous SQL failed with this "
                                    f"database error: {db_error}\n"
                                    f"Previous failing SQL: {sql}\n"
                                    f"Please generate a corrected query that avoids "
                                    f"this error."
                                )
                                gen_elapsed = time.monotonic() - retry_start
                            except (UnsafeSQLError, SQLGenerationError) as exc:
                                st.error(f"**Retry failed:** {exc}")
                                sql = None
                                break
                        continue

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
                    break

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

# ---- Auto Monitor tab -----------------------------------------------------
with tab_monitor:
    st.subheader("📡 Tablespace Auto Monitor")

    if not (st.session_state.db_client and st.session_state.db_client.is_connected):
        st.warning("Connect to a database first.")
    else:
        db_client = st.session_state.db_client

        st.markdown(
            "Periodically monitors tablespace usage and automatically extends "
            "datafiles when usage exceeds the threshold (Oracle). "
            "For PostgreSQL, reports storage metrics."
        )

        mcol1, mcol2, mcol3 = st.columns(3)
        with mcol1:
            mon_threshold = st.slider(
                "Usage threshold (%)", 50, 99, 85, key="mon_threshold"
            )
        with mcol2:
            mon_interval = st.selectbox(
                "Check interval",
                [60, 300, 900, 1800, 3600],
                index=4,
                format_func=lambda x: (
                    f"{x // 60} min" if x < 3600 else f"{x // 3600} hr"
                ),
                key="mon_interval",
            )
        with mcol3:
            mon_max_gb = st.number_input(
                "Max file size (GB)", 1, 100, 20, key="mon_max_gb"
            )

        bcol1, bcol2, bcol3 = st.columns(3)
        with bcol1:
            if st.button(
                "▶️ Start Auto Monitor", use_container_width=True, type="primary"
            ):
                monitor = TablespaceMonitor(
                    db_client=db_client,
                    threshold_pct=mon_threshold,
                    max_file_size_gb=mon_max_gb,
                    interval_sec=mon_interval,
                )
                monitor.start()
                st.session_state.monitor = monitor
                st.success("Monitor started!")
        with bcol2:
            if st.button("⏹️ Stop Monitor", use_container_width=True):
                if st.session_state.monitor:
                    st.session_state.monitor.stop()
                    st.info("Monitor stopped.")
        with bcol3:
            if st.button("🔍 Check Now", use_container_width=True):
                monitor = st.session_state.monitor
                if not monitor:
                    monitor = TablespaceMonitor(
                        db_client=db_client,
                        threshold_pct=mon_threshold,
                        max_file_size_gb=mon_max_gb,
                    )
                    st.session_state.monitor = monitor
                with st.spinner("Checking tablespace usage..."):
                    event = monitor.run_check()
                st.success("Check complete!")

        if st.session_state.monitor and st.session_state.monitor.running:
            st.info(
                f"Monitor is running (interval: {st.session_state.monitor.interval_sec}s, "
                f"threshold: {st.session_state.monitor.threshold_pct}%)"
            )

        # Display monitor events
        monitor = st.session_state.monitor
        if monitor and monitor.events:
            st.divider()
            st.subheader("Monitor Events")
            for i, evt in enumerate(reversed(monitor.events[-20:])):
                status_icon = {"ok": "🟢", "warning": "🟡", "error": "🔴"}.get(
                    evt["status"], "⚪"
                )
                with st.expander(
                    f"{status_icon} {evt['timestamp']} - {evt['status'].upper()}"
                ):
                    if evt.get("error"):
                        st.error(evt["error"])

                    ts_data = evt.get("tablespace_data", [])
                    display_rows = [
                        r
                        for r in ts_data
                        if isinstance(r, dict) and "_section" not in r
                    ]
                    if display_rows:
                        st.caption("Tablespace Usage")
                        st.dataframe(
                            pd.DataFrame(display_rows),
                            use_container_width=True,
                            hide_index=True,
                        )

                    for section_item in ts_data:
                        if (
                            isinstance(section_item, dict)
                            and "_section" in section_item
                        ):
                            st.caption(section_item["_section"].title())
                            sec_rows = section_item.get("rows", [])
                            if sec_rows:
                                st.dataframe(
                                    pd.DataFrame(sec_rows),
                                    use_container_width=True,
                                    hide_index=True,
                                )

                    actions = evt.get("actions", [])
                    if actions:
                        st.caption("Actions Taken")
                        for act in actions:
                            act_icon = (
                                "✅"
                                if "added" in act.get("action", "")
                                or "enabled" in act.get("action", "")
                                else "❌"
                            )
                            st.markdown(f"{act_icon} **{act.get('action', '')}**")
                            if act.get("sql"):
                                st.code(act["sql"], language="sql")
                            if act.get("error"):
                                st.error(act["error"])

# ---- Auto Analyse tab -----------------------------------------------------
with tab_analyse:
    st.subheader("📊 Performance Analysis")

    if not (st.session_state.db_client and st.session_state.db_client.is_connected):
        st.warning("Connect to a database first.")
    elif not st.session_state.llm_client:
        st.warning("Configure Ollama settings and connect first.")
    else:
        db_client = st.session_state.db_client
        llm_client = st.session_state.llm_client
        db_label = db_client.db_type.upper()
        is_oracle = db_client.db_type == DB_TYPE_ORACLE

        st.markdown(
            f"Collects performance data from **{db_label}** "
            f"({'AWR / V$ views' if is_oracle else 'pg_stat_statements / pg_stat_* / pgProfile'}) "
            "and generates an AI-powered summary with action plan."
        )

        # Analysis mode selector
        if is_oracle:
            analyse_mode = st.radio(
                "Analysis mode",
                [
                    "Live V$ views",
                    "AWR Snap ID range",
                    "Upload report file",
                ],
                horizontal=True,
                key="analyse_mode",
            )
        else:
            analyse_mode = st.radio(
                "Analysis mode",
                [
                    "Live pg_stat_* views",
                    "pgProfile Snap ID range",
                    "Latest pg_stat_statements",
                    "Upload report file",
                ],
                horizontal=True,
                key="analyse_mode",
            )

        st.divider()

        # ------- Mode: Live V$ / pg_stat_* -----------------------------------
        if analyse_mode in ("Live V$ views", "Live pg_stat_* views"):
            acol1, acol2 = st.columns(2)
            with acol1:
                if st.button("📈 Collect Data Only", use_container_width=True):
                    analyser = PerformanceAnalyser(
                        db_client=db_client, llm_client=llm_client
                    )
                    with st.spinner("Collecting performance data..."):
                        raw_data = analyser.collect_data()
                    st.session_state.analyser = analyser
                    st.session_state["_last_analysis"] = {
                        "raw_data": raw_data,
                        "analysis": None,
                    }
                    st.success("Data collected!")

            with acol2:
                if st.button(
                    "🧠 Full Analysis (Data + LLM)",
                    use_container_width=True,
                    type="primary",
                ):
                    analyser = PerformanceAnalyser(
                        db_client=db_client, llm_client=llm_client
                    )
                    with st.spinner("Collecting data and running LLM analysis..."):
                        result = analyser.analyse()
                    st.session_state.analyser = analyser
                    st.session_state["_last_analysis"] = result
                    st.success("Analysis complete!")

        # ------- Mode: AWR Snap ID range (Oracle) ----------------------------
        elif analyse_mode == "AWR Snap ID range":
            analyser = PerformanceAnalyser(db_client=db_client, llm_client=llm_client)
            st.markdown("Select an AWR snapshot range from `DBA_HIST_SNAPSHOT`.")

            if st.button("🔄 Load AWR Snapshots"):
                with st.spinner("Querying DBA_HIST_SNAPSHOT..."):
                    snaps = analyser.list_awr_snapshots()
                st.session_state["_awr_snapshots"] = snaps

            snaps = st.session_state.get("_awr_snapshots", [])
            if snaps:
                snap_df = pd.DataFrame(snaps)
                st.dataframe(
                    snap_df, use_container_width=True, hide_index=True, height=250
                )
                snap_ids = [int(s["snap_id"]) for s in snaps]
                scol1, scol2 = st.columns(2)
                with scol1:
                    begin_snap = st.selectbox(
                        "Begin Snap ID",
                        sorted(snap_ids),
                        index=max(0, len(snap_ids) - 2),
                        key="awr_begin",
                    )
                with scol2:
                    end_snap = st.selectbox(
                        "End Snap ID",
                        sorted(snap_ids),
                        index=len(snap_ids) - 1,
                        key="awr_end",
                    )

                if st.button(
                    "🧠 Analyse AWR Range",
                    use_container_width=True,
                    type="primary",
                ):
                    if begin_snap >= end_snap:
                        st.error("Begin Snap ID must be less than End Snap ID.")
                    else:
                        with st.spinner(
                            f"Collecting AWR data for snaps {begin_snap}–{end_snap}..."
                        ):
                            result = analyser.analyse_awr_snaps(begin_snap, end_snap)
                        st.session_state.analyser = analyser
                        st.session_state["_last_analysis"] = result
                        st.success("AWR analysis complete!")
            else:
                st.info("Click 'Load AWR Snapshots' to list available snapshot IDs.")

        # ------- Mode: pgProfile Snap ID range (PostgreSQL) ------------------
        elif analyse_mode == "pgProfile Snap ID range":
            analyser = PerformanceAnalyser(db_client=db_client, llm_client=llm_client)
            st.markdown(
                "Select a pgProfile sample range from `profile.samples`. "
                "Requires the [pgProfile](https://github.com/zubkov-andrei/pg_profile) extension."
            )

            if st.button("🔄 Load pgProfile Samples"):
                with st.spinner("Querying profile.samples..."):
                    samples = analyser.list_pgprofile_samples()
                if not samples:
                    st.warning(
                        "No pgProfile samples found. Is the pgProfile extension "
                        "installed and configured?"
                    )
                st.session_state["_pgprofile_samples"] = samples

            samples = st.session_state.get("_pgprofile_samples", [])
            if samples:
                samp_df = pd.DataFrame(samples)
                st.dataframe(
                    samp_df, use_container_width=True, hide_index=True, height=250
                )
                sample_ids = [int(s["sample_id"]) for s in samples]
                pcol1, pcol2 = st.columns(2)
                with pcol1:
                    begin_sample = st.selectbox(
                        "Begin Sample ID",
                        sorted(sample_ids),
                        index=max(0, len(sample_ids) - 2),
                        key="pgp_begin",
                    )
                with pcol2:
                    end_sample = st.selectbox(
                        "End Sample ID",
                        sorted(sample_ids),
                        index=len(sample_ids) - 1,
                        key="pgp_end",
                    )

                if st.button(
                    "🧠 Analyse pgProfile Range",
                    use_container_width=True,
                    type="primary",
                ):
                    if begin_sample >= end_sample:
                        st.error("Begin Sample ID must be less than End Sample ID.")
                    else:
                        with st.spinner(
                            f"Collecting pgProfile data for samples "
                            f"{begin_sample}–{end_sample}..."
                        ):
                            result = analyser.analyse_pgprofile_snaps(
                                begin_sample, end_sample
                            )
                        st.session_state.analyser = analyser
                        st.session_state["_last_analysis"] = result
                        st.success("pgProfile analysis complete!")
            else:
                st.info("Click 'Load pgProfile Samples' to list available sample IDs.")

        # ------- Mode: Latest pg_stat_statements (PostgreSQL) ----------------
        elif analyse_mode == "Latest pg_stat_statements":
            analyser = PerformanceAnalyser(db_client=db_client, llm_client=llm_client)
            st.markdown(
                "Collects the **latest cumulative snapshot** from "
                "`pg_stat_statements` plus table, database, bgwriter stats "
                "and unused indexes."
            )

            if st.button(
                "🧠 Analyse Latest pg_stat_statements",
                use_container_width=True,
                type="primary",
            ):
                with st.spinner("Checking pg_stat_statements extension..."):
                    has_ext = analyser.check_pg_stat_statements()
                if not has_ext:
                    st.error(
                        "pg_stat_statements extension is not installed. "
                        "Run `CREATE EXTENSION pg_stat_statements;` first."
                    )
                else:
                    with st.spinner(
                        "Collecting pg_stat_statements data and running LLM analysis..."
                    ):
                        result = analyser.analyse_pg_stat_latest()
                    st.session_state.analyser = analyser
                    st.session_state["_last_analysis"] = result
                    st.success("pg_stat_statements analysis complete!")

        # ------- Mode: Upload report file ------------------------------------
        elif analyse_mode == "Upload report file":
            st.markdown(
                "Upload an **AWR report** (HTML/text), **pg_stat_statements CSV**, "
                "or **pgProfile report** (HTML/text) for LLM-powered analysis."
            )
            uploaded_file = st.file_uploader(
                "Choose a report file",
                type=["html", "htm", "txt", "csv", "log"],
                key="report_upload",
            )
            if uploaded_file is not None:
                if st.button(
                    "🧠 Analyse Uploaded Report",
                    use_container_width=True,
                    type="primary",
                ):
                    analyser = PerformanceAnalyser(
                        db_client=db_client, llm_client=llm_client
                    )
                    file_content = uploaded_file.getvalue().decode(
                        "utf-8", errors="replace"
                    )
                    with st.spinner(f"Parsing and analysing {uploaded_file.name}..."):
                        result = analyser.analyse_uploaded_report(
                            file_content, uploaded_file.name
                        )
                    st.session_state.analyser = analyser
                    st.session_state["_last_analysis"] = result
                    st.success("Report analysis complete!")

        # ------- Display analysis results (shared across all modes) ----------
        last = st.session_state.get("_last_analysis")
        if last:
            st.divider()

            if last.get("analysis"):
                st.subheader("AI Analysis & Action Plan")
                st.markdown(last["analysis"])

            raw = last.get("raw_data", {})
            if raw:
                st.divider()
                st.subheader("Raw Performance Data")
                for section_name, section_data in raw.items():
                    if section_name in ("db_type", "snap_range", "sample_range"):
                        continue
                    label = section_name.replace("_", " ").title()
                    with st.expander(f"📊 {label}"):
                        if isinstance(section_data, dict) and "error" in section_data:
                            st.error(section_data["error"])
                        elif isinstance(section_data, list) and section_data:
                            st.dataframe(
                                pd.DataFrame(section_data),
                                use_container_width=True,
                                hide_index=True,
                            )
                        else:
                            st.info("No data available.")

            if last.get("report_text") and not raw:
                with st.expander("📄 Parsed Report Text"):
                    st.text(last["report_text"][:5000])

# ---- Sessions & Locks tab -------------------------------------------------
with tab_sessions:
    st.subheader("🔒 Session & Lock Monitor")

    if not (st.session_state.db_client and st.session_state.db_client.is_connected):
        st.warning("Connect to a database first.")
    else:
        db_client = st.session_state.db_client
        monitor = SessionMonitor(db_client)
        is_oracle = db_client.db_type == DB_TYPE_ORACLE

        sess_view = st.radio(
            "View",
            [
                "Active Sessions",
                "Blocking Lock Tree",
                "Lock Details",
                "Long-Running Queries",
                "Wait Events",
            ],
            horizontal=True,
            key="sess_view",
        )

        if st.button("🔄 Refresh", key="sess_refresh"):
            st.session_state["_sess_data"] = None

        # Fetch data based on selected view
        with st.spinner("Querying sessions..."):
            if sess_view == "Active Sessions":
                result = monitor.get_active_sessions()
            elif sess_view == "Blocking Lock Tree":
                result = monitor.get_blocking_tree()
            elif sess_view == "Lock Details":
                result = monitor.get_lock_details()
            elif sess_view == "Long-Running Queries":
                result = monitor.get_long_running()
            else:
                result = monitor.get_wait_events()

        if "error" in result:
            st.error(result["error"])
        else:
            rows = result.get("rows", [])
            if rows:
                st.caption(f"{len(rows)} row(s)")
                st.dataframe(
                    pd.DataFrame(rows),
                    use_container_width=True,
                    hide_index=True,
                )

                # Kill session UI
                st.divider()
                st.subheader("Kill / Cancel Session")
                st.warning(
                    "Use with caution. This will terminate the selected session."
                )
                kcol1, kcol2, kcol3 = st.columns([2, 2, 2])

                if is_oracle:
                    with kcol1:
                        kill_sid = st.number_input(
                            "SID", min_value=1, step=1, key="kill_sid"
                        )
                    with kcol2:
                        kill_serial = st.number_input(
                            "Serial#", min_value=1, step=1, key="kill_serial"
                        )
                    with kcol3:
                        if st.button(
                            "⚠️ Kill Session (Oracle)",
                            type="primary",
                            key="kill_ora",
                        ):
                            kill_result = monitor.kill_session(kill_sid, kill_serial)
                            if kill_result.get("success"):
                                st.success(f"Session {kill_sid},{kill_serial} killed.")
                            else:
                                st.error(kill_result.get("error", "Kill failed"))
                else:
                    with kcol1:
                        kill_pid = st.number_input(
                            "PID", min_value=1, step=1, key="kill_pid"
                        )
                    with kcol2:
                        kill_force = st.checkbox(
                            "Force terminate (pg_terminate_backend)",
                            key="kill_force",
                        )
                    with kcol3:
                        label = "⚠️ Terminate Backend" if kill_force else "Cancel Query"
                        if st.button(label, type="primary", key="kill_pg"):
                            kill_result = monitor.kill_session(
                                kill_pid, force=kill_force
                            )
                            if "error" in kill_result:
                                st.error(kill_result["error"])
                            else:
                                st.success(
                                    f"PID {kill_pid} "
                                    f"{'terminated' if kill_force else 'cancel sent'}."
                                )
            else:
                st.info("No sessions/locks found for this view.")

# ---- SQL Tuning Advisor tab -----------------------------------------------
with tab_tuning:
    st.subheader("🔧 SQL Tuning Advisor")
    st.markdown(
        "Paste a SQL statement to get its **execution plan**, table metadata, "
        "and **LLM-powered tuning recommendations** (index suggestions, "
        "SQL rewrites, stats maintenance)."
    )

    if not (st.session_state.db_client and st.session_state.db_client.is_connected):
        st.warning("Connect to a database first.")
    elif not st.session_state.llm_client:
        st.warning("Configure Ollama settings and connect first.")
    else:
        db_client = st.session_state.db_client
        llm_client = st.session_state.llm_client
        is_oracle = db_client.db_type == DB_TYPE_ORACLE

        tune_sql = st.text_area(
            "SQL to tune",
            height=200,
            placeholder=(
                "SELECT o.order_id, c.customer_name, p.product_name\n"
                "FROM orders o\n"
                "JOIN customers c ON o.customer_id = c.id\n"
                "JOIN products p ON o.product_id = p.id\n"
                "WHERE o.order_date > '2024-01-01'\n"
                "ORDER BY o.order_date DESC"
            ),
            key="tune_sql_input",
        )

        tcol1, tcol2 = st.columns(2)
        with tcol1:
            if not is_oracle:
                run_analyze = st.checkbox(
                    "Use EXPLAIN ANALYZE (executes the query — use with caution)",
                    key="tune_analyze",
                )
            else:
                run_analyze = False

        with tcol2:
            tune_btn = st.button(
                "🔧 Analyse & Tune",
                use_container_width=True,
                type="primary",
                key="tune_btn",
            )

        if tune_btn and tune_sql.strip():
            advisor = SQLTuningAdvisor(db_client=db_client, llm_client=llm_client)
            with st.spinner(
                "Running EXPLAIN, collecting metadata, analysing with LLM..."
            ):
                result = advisor.analyse_sql(tune_sql.strip(), run_analyze=run_analyze)

            if result.get("error"):
                st.error(result["error"])
            else:
                # Show execution plan
                plan_text = result.get("plan_text", "")
                if plan_text:
                    st.subheader("Execution Plan")
                    st.code(plan_text, language="text")

                # Show LLM analysis
                analysis = result.get("analysis", "")
                if analysis:
                    st.divider()
                    st.subheader("AI Tuning Recommendations")
                    st.markdown(analysis)

                # Show raw metadata in expander
                metadata = result.get("metadata", {})
                table_meta = metadata.get("table_metadata", "")
                if table_meta:
                    with st.expander("📋 Table Metadata (columns, indexes, stats)"):
                        st.text(table_meta[:8000])

        elif tune_btn:
            st.warning("Please enter a SQL statement to tune.")

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
