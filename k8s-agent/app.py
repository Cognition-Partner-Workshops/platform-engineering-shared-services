"""K8s Agent — Streamlit-based Kubernetes Cluster Management UI."""

import sys
import os

# Ensure the k8s-agent directory is on the Python path so sibling imports work.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import streamlit as st
from streamlit_option_menu import option_menu

import config
from modules.profile_manager import (
    ClusterProfile,
    save_profile,
    load_profile,
    list_profiles,
    delete_profile,
    update_profile_status,
)
from modules.cluster_creator import (
    test_ssh_connectivity,
    generate_common_setup_script,
    generate_control_plane_init_script,
    generate_worker_join_script,
    generate_best_practices_script,
    provision_node_common,
    init_control_plane,
    retrieve_join_command,
    join_worker_node,
    apply_best_practices,
    get_cluster_status,
    get_llm_cluster_advice,
)
from modules.cluster_debugger import (
    DIAGNOSTIC_COMMANDS,
    CATEGORY_MAP,
    run_diagnostic,
    run_category_diagnostics,
    run_all_diagnostics,
    run_custom_command,
    analyze_diagnostics,
    get_debug_suggestion,
    check_pod_issues,
)
from modules.monitoring_setup import (
    GRAFANA_DASHBOARDS,
    install_helm,
    install_prometheus_stack,
    install_dashboards,
    install_alert_rules,
    get_monitoring_status,
    get_monitoring_advice,
    generate_prometheus_install_script,
    generate_dashboard_import_script,
    generate_alerting_rules_script,
)
from modules.log_analyzer import (
    LOG_SOURCES,
    collect_logs,
    collect_pod_logs,
    collect_multi_source_logs,
    analyze_logs,
    correlate_errors,
    llm_analyze_logs,
    llm_correlate_analysis,
    get_pod_list,
)
from modules.llm_client import query_llm, stream_llm


# ── Page Configuration ────────────────────────────────────────────────────

st.set_page_config(
    page_title="K8s Agent",
    page_icon="☸",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Custom CSS ────────────────────────────────────────────────────────────

st.markdown("""
<style>
    .main-header {
        font-size: 2rem;
        font-weight: 700;
        color: #326CE5;
        margin-bottom: 0.5rem;
    }
    .sub-header {
        font-size: 1rem;
        color: #666;
        margin-bottom: 1.5rem;
    }
    .status-active { color: #28a745; font-weight: bold; }
    .status-error { color: #dc3545; font-weight: bold; }
    .status-draft { color: #6c757d; font-weight: bold; }
    .status-provisioning { color: #fd7e14; font-weight: bold; }
    .node-card {
        border: 1px solid #ddd;
        border-radius: 8px;
        padding: 1rem;
        margin: 0.5rem 0;
        background: #f8f9fa;
    }
    .metric-card {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 10px;
        padding: 1.2rem;
        color: white;
        text-align: center;
    }
    .metric-value { font-size: 2rem; font-weight: 700; }
    .metric-label { font-size: 0.85rem; opacity: 0.9; }
    div[data-testid="stExpander"] details summary p {
        font-size: 1rem;
        font-weight: 600;
    }
</style>
""", unsafe_allow_html=True)


# ── Session state initialization ──────────────────────────────────────────

def init_session_state():
    defaults = {
        "active_profile": None,
        "chat_history": [],
        "provisioning_log": [],
        "debug_results": {},
        "log_analysis_results": {},
    }
    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


init_session_state()


# ── Sidebar: Profile Manager + Navigation ─────────────────────────────────

def render_sidebar():
    with st.sidebar:
        st.markdown('<div class="main-header">☸ K8s Agent</div>', unsafe_allow_html=True)
        st.markdown('<div class="sub-header">On-Prem Kubernetes Management</div>', unsafe_allow_html=True)

        st.divider()

        # ── Profile selector ──
        st.markdown("### Cluster Profiles")
        profiles = list_profiles()
        profile_names = [p.name for p in profiles]

        if profile_names:
            selected = st.selectbox(
                "Active Profile",
                options=["(none)"] + profile_names,
                index=(
                    profile_names.index(st.session_state.active_profile) + 1
                    if st.session_state.active_profile in profile_names
                    else 0
                ),
                key="profile_selector",
            )
            if selected != "(none)":
                st.session_state.active_profile = selected
                profile = load_profile(selected)
                if profile:
                    status_class = f"status-{profile.status}"
                    st.markdown(
                        f"**Status:** <span class='{status_class}'>{profile.status.upper()}</span>",
                        unsafe_allow_html=True,
                    )
                    st.caption(
                        f"K8s {profile.kubernetes_version} | CRI-O {profile.crio_version} | "
                        f"{len(profile.get_control_plane_nodes())} CP + "
                        f"{len(profile.get_worker_nodes())} Workers"
                    )
            else:
                st.session_state.active_profile = None
        else:
            st.info("No profiles yet. Create one in Profile Manager.")

        st.divider()

        # ── Navigation ──
        selected_page = option_menu(
            menu_title="Navigation",
            options=[
                "Profile Manager",
                "Cluster Creation",
                "Cluster Debugger",
                "Monitoring Setup",
                "Log Analysis",
                "AI Assistant",
            ],
            icons=[
                "person-gear",
                "hdd-rack",
                "bug",
                "graph-up",
                "journal-text",
                "robot",
            ],
            menu_icon="list",
            default_index=0,
        )

        st.divider()

        # ── LLM config ──
        with st.expander("LLM Settings"):
            st.text_input(
                "API URL",
                value=config.LLM_API_URL,
                key="llm_api_url",
                help="Endpoint for the LLM API",
            )
            st.text_input(
                "API Key",
                value=config.LLM_API_KEY[:8] + "..." if config.LLM_API_KEY else "",
                type="password",
                key="llm_api_key_display",
                disabled=True,
                help="Set via LLM_API_KEY or INFOSYS_CODER_API_KEY env var",
            )
            st.selectbox(
                "Model",
                options=["gpt-4", "gpt-4o", "gpt-3.5-turbo"],
                index=0,
                key="llm_model_select",
            )

        return selected_page


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Profile Manager
# ══════════════════════════════════════════════════════════════════════════

def page_profile_manager():
    st.markdown("## Cluster Profile Manager")
    st.markdown("Create, edit, and manage profiles for your on-prem Kubernetes clusters.")

    tab_create, tab_list, tab_import = st.tabs(["Create Profile", "Manage Profiles", "Import / Export"])

    # ── Create Profile ────────────────────────────────────────────────────
    with tab_create:
        with st.form("create_profile_form"):
            st.markdown("### New Cluster Profile")
            col1, col2 = st.columns(2)

            with col1:
                name = st.text_input("Profile Name *", placeholder="production-cluster")
                description = st.text_area("Description", placeholder="Production on-prem cluster")
                k8s_version = st.selectbox("Kubernetes Version", ["1.30", "1.29", "1.28", "1.27"], index=0)
                crio_version = st.selectbox("CRI-O Version", ["1.30", "1.29", "1.28", "1.27"], index=0)
                pod_security = st.selectbox(
                    "Pod Security Standard",
                    ["restricted", "baseline", "privileged"],
                    index=0,
                )

            with col2:
                pod_cidr = st.text_input("Pod CIDR", value="10.244.0.0/16")
                service_cidr = st.text_input("Service CIDR", value="10.96.0.0/12")
                dns_domain = st.text_input("DNS Domain", value="cluster.local")

            st.divider()
            st.markdown("### Nodes")
            st.markdown("Define your control-plane and worker nodes.")

            num_nodes = st.number_input("Number of Nodes", min_value=1, max_value=50, value=3, step=1)

            nodes = []
            for i in range(int(num_nodes)):
                st.markdown(f"**Node {i + 1}**")
                ncol1, ncol2, ncol3, ncol4, ncol5 = st.columns([2, 2, 1.5, 1, 1.5])
                with ncol1:
                    hostname = st.text_input(f"Hostname", key=f"host_{i}", placeholder=f"node-{i + 1}")
                with ncol2:
                    ip_addr = st.text_input(f"IP Address", key=f"ip_{i}", placeholder="192.168.1.x")
                with ncol3:
                    role = st.selectbox(f"Role", ["control-plane", "worker"], key=f"role_{i}",
                                        index=0 if i == 0 else 1)
                with ncol4:
                    ssh_user = st.text_input(f"SSH User", key=f"user_{i}", value="root")
                with ncol5:
                    ssh_key = st.text_input(f"SSH Key Path", key=f"key_{i}", value="~/.ssh/id_rsa")

                nodes.append({
                    "hostname": hostname,
                    "ip_address": ip_addr,
                    "role": role,
                    "ssh_user": ssh_user,
                    "ssh_port": 22,
                    "ssh_key_path": ssh_key,
                })

            submitted = st.form_submit_button("Create Profile", type="primary", use_container_width=True)

            if submitted:
                if not name:
                    st.error("Profile name is required.")
                elif not any(n["ip_address"] for n in nodes):
                    st.error("At least one node must have an IP address.")
                elif not any(n["role"] == "control-plane" for n in nodes):
                    st.error("At least one control-plane node is required.")
                else:
                    valid_nodes = [n for n in nodes if n["ip_address"]]
                    profile = ClusterProfile(
                        name=name,
                        description=description,
                        kubernetes_version=k8s_version,
                        crio_version=crio_version,
                        cni_plugin="flannel",
                        pod_cidr=pod_cidr,
                        service_cidr=service_cidr,
                        dns_domain=dns_domain,
                        nodes=valid_nodes,
                        pod_security_standard=pod_security,
                    )
                    path = save_profile(profile)
                    st.session_state.active_profile = name
                    st.success(f"Profile '{name}' created successfully!")
                    st.rerun()

    # ── Manage Profiles ───────────────────────────────────────────────────
    with tab_list:
        profiles = list_profiles()
        if not profiles:
            st.info("No profiles created yet.")
            return

        for profile in profiles:
            with st.expander(f"**{profile.name}** — {profile.status.upper()}", expanded=False):
                col1, col2, col3 = st.columns([2, 2, 1])
                with col1:
                    st.markdown(f"**Description:** {profile.description or 'N/A'}")
                    st.markdown(f"**Kubernetes:** {profile.kubernetes_version} | **CRI-O:** {profile.crio_version}")
                    st.markdown(f"**Pod CIDR:** {profile.pod_cidr} | **Service CIDR:** {profile.service_cidr}")
                    st.markdown(f"**Pod Security:** {profile.pod_security_standard}")
                with col2:
                    st.markdown("**Nodes:**")
                    for node in profile.nodes:
                        icon = "🔵" if node["role"] == "control-plane" else "🟢"
                        st.markdown(
                            f"{icon} `{node.get('hostname', 'N/A')}` — "
                            f"`{node['ip_address']}` ({node['role']})"
                        )
                with col3:
                    st.markdown(f"**Created:** {profile.created_at[:10] if profile.created_at else 'N/A'}")
                    st.markdown(f"**Updated:** {profile.updated_at[:10] if profile.updated_at else 'N/A'}")
                    if st.button("Set Active", key=f"activate_{profile.name}"):
                        st.session_state.active_profile = profile.name
                        st.rerun()
                    if st.button("Delete", key=f"delete_{profile.name}", type="secondary"):
                        delete_profile(profile.name)
                        if st.session_state.active_profile == profile.name:
                            st.session_state.active_profile = None
                        st.rerun()

    # ── Import / Export ───────────────────────────────────────────────────
    with tab_import:
        col_export, col_import = st.columns(2)
        with col_export:
            st.markdown("### Export Profile")
            profiles = list_profiles()
            if profiles:
                export_name = st.selectbox("Select profile to export", [p.name for p in profiles])
                if st.button("Export as JSON"):
                    profile = load_profile(export_name)
                    if profile:
                        from dataclasses import asdict
                        st.download_button(
                            label="Download JSON",
                            data=json.dumps(asdict(profile), indent=2),
                            file_name=f"{export_name}.json",
                            mime="application/json",
                        )

        with col_import:
            st.markdown("### Import Profile")
            uploaded = st.file_uploader("Upload profile JSON", type=["json"])
            if uploaded:
                try:
                    data = json.loads(uploaded.read())
                    profile = ClusterProfile(**data)
                    save_profile(profile)
                    st.success(f"Profile '{profile.name}' imported!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to import: {e}")


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Cluster Creation
# ══════════════════════════════════════════════════════════════════════════

def page_cluster_creation():
    st.markdown("## Cluster Creation")
    st.markdown("Provision an on-prem K8s cluster via SSH with CRI-O, Flannel CNI, and best practices.")

    profile = _get_active_profile()
    if not profile:
        return

    _show_profile_summary(profile)

    tab_preflight, tab_provision, tab_scripts, tab_advice = st.tabs([
        "Pre-flight Checks",
        "Provision Cluster",
        "View Scripts",
        "AI Advice",
    ])

    # ── Pre-flight: SSH connectivity ──────────────────────────────────────
    with tab_preflight:
        st.markdown("### SSH Connectivity Test")
        st.markdown("Test SSH access to all nodes before provisioning.")

        if st.button("Test All Nodes", type="primary"):
            for node in profile.nodes:
                with st.status(f"Testing {node.get('hostname', node['ip_address'])}...", expanded=True):
                    result = test_ssh_connectivity(node)
                    if result.success:
                        st.success(f"Connected to {node['ip_address']}")
                        st.code(result.stdout, language="text")
                    else:
                        st.error(f"Failed to connect to {node['ip_address']}")
                        st.code(result.stderr, language="text")

    # ── Provision ─────────────────────────────────────────────────────────
    with tab_provision:
        st.markdown("### Automated Cluster Provisioning")
        st.warning(
            "This will SSH into each node and install Kubernetes components. "
            "Ensure all nodes are accessible and you have root/sudo access."
        )

        cp_nodes = profile.get_control_plane_nodes()
        worker_nodes = profile.get_worker_nodes()

        st.markdown(f"**Control Plane:** {len(cp_nodes)} node(s) | **Workers:** {len(worker_nodes)} node(s)")

        col1, col2, col3 = st.columns(3)
        with col1:
            step1 = st.checkbox("Step 1: Common Setup (all nodes)", value=True)
        with col2:
            step2 = st.checkbox("Step 2: Init Control Plane", value=True)
        with col3:
            step3 = st.checkbox("Step 3: Join Workers", value=True)
        step4 = st.checkbox("Step 4: Apply Best Practices", value=True)

        if st.button("Start Provisioning", type="primary", use_container_width=True):
            update_profile_status(profile.name, "provisioning")

            # Step 1: Common setup on all nodes
            if step1:
                st.markdown("---")
                st.markdown("### Step 1: Common Setup")
                for node in profile.nodes:
                    with st.status(
                        f"Setting up {node.get('hostname', node['ip_address'])} ({node['role']})...",
                        expanded=True,
                    ):
                        result = provision_node_common(node, profile)
                        if result.success:
                            st.success(f"Common setup complete on {node['ip_address']}")
                        else:
                            st.error(f"Setup failed on {node['ip_address']}")
                            st.code(result.stderr, language="text")

            # Step 2: Initialize control plane
            if step2 and cp_nodes:
                st.markdown("---")
                st.markdown("### Step 2: Control Plane Initialization")
                cp_node = cp_nodes[0]
                with st.status(f"Initializing control plane on {cp_node['ip_address']}...", expanded=True):
                    result = init_control_plane(cp_node, profile)
                    if result.success:
                        st.success("Control plane initialized!")
                        st.code(result.stdout[-2000:], language="text")
                    else:
                        st.error("Control plane initialization failed!")
                        st.code(result.stderr, language="text")

            # Step 3: Join worker nodes
            if step3 and worker_nodes and cp_nodes:
                st.markdown("---")
                st.markdown("### Step 3: Join Worker Nodes")
                join_cmd = retrieve_join_command(cp_nodes[0])
                if join_cmd:
                    for node in worker_nodes:
                        with st.status(f"Joining {node.get('hostname', node['ip_address'])}...", expanded=True):
                            result = join_worker_node(node, join_cmd)
                            if result.success:
                                st.success(f"Worker {node['ip_address']} joined!")
                            else:
                                st.error(f"Failed to join {node['ip_address']}")
                                st.code(result.stderr, language="text")
                else:
                    st.error("Could not retrieve join command from control plane.")

            # Step 4: Best practices
            if step4 and cp_nodes:
                st.markdown("---")
                st.markdown("### Step 4: Best Practices")
                with st.status("Applying security and resource best practices...", expanded=True):
                    result = apply_best_practices(cp_nodes[0])
                    if result.success:
                        st.success("Best practices applied!")
                        st.code(result.stdout, language="text")
                    else:
                        st.error("Failed to apply best practices")
                        st.code(result.stderr, language="text")

            # Final status
            st.markdown("---")
            st.markdown("### Cluster Status")
            if cp_nodes:
                result = get_cluster_status(cp_nodes[0])
                if result.success:
                    update_profile_status(profile.name, "active")
                    st.success("Cluster is active!")
                    st.code(result.stdout, language="text")
                else:
                    update_profile_status(profile.name, "error")
                    st.error("Could not verify cluster status")
                    st.code(result.stderr, language="text")

    # ── View Scripts ──────────────────────────────────────────────────────
    with tab_scripts:
        st.markdown("### Generated Scripts")
        st.markdown("Review the scripts that will be executed during provisioning.")

        with st.expander("Common Setup Script (all nodes)", expanded=False):
            st.code(generate_common_setup_script(profile), language="bash")

        with st.expander("Control Plane Init Script", expanded=False):
            st.code(generate_control_plane_init_script(profile), language="bash")

        with st.expander("Worker Join Script", expanded=False):
            st.code(generate_worker_join_script(), language="bash")

        with st.expander("Best Practices Script", expanded=False):
            st.code(generate_best_practices_script(), language="bash")

    # ── AI Advice ─────────────────────────────────────────────────────────
    with tab_advice:
        st.markdown("### AI Cluster Setup Advisor")
        context = st.text_area(
            "Additional context or questions",
            placeholder="e.g., We have 3 nodes with 16GB RAM each. Any special considerations?",
        )
        if st.button("Get AI Recommendations", type="primary"):
            with st.spinner("Analyzing your cluster configuration..."):
                advice = get_llm_cluster_advice(profile, context)
                st.markdown(advice)


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Cluster Debugger
# ══════════════════════════════════════════════════════════════════════════

def page_cluster_debugger():
    st.markdown("## Cluster Debugger")
    st.markdown("Diagnose issues and get AI-powered recommendations.")

    profile = _get_active_profile()
    if not profile:
        return

    cp_nodes = profile.get_control_plane_nodes()
    if not cp_nodes:
        st.error("No control-plane node defined in this profile.")
        return
    cp_node = cp_nodes[0]

    tab_quick, tab_category, tab_custom, tab_ai = st.tabs([
        "Quick Diagnostics",
        "Category Scan",
        "Custom Command",
        "AI Debug Assistant",
    ])

    # ── Quick Diagnostics ─────────────────────────────────────────────────
    with tab_quick:
        st.markdown("### Quick Diagnostic Checks")
        col1, col2 = st.columns(2)
        with col1:
            selected_checks = st.multiselect(
                "Select checks to run",
                options=list(DIAGNOSTIC_COMMANDS.keys()),
                default=["Node Status", "Pod Status (All Namespaces)", "Events (Recent)"],
            )
        with col2:
            run_all = st.checkbox("Run ALL diagnostics")

        if st.button("Run Diagnostics", type="primary"):
            if run_all:
                with st.spinner("Running all diagnostics..."):
                    results = run_all_diagnostics(cp_node)
            else:
                results = {}
                for check in selected_checks:
                    with st.spinner(f"Running: {check}..."):
                        results[check] = run_diagnostic(cp_node, check)

            st.session_state.debug_results = results

            for name, result in results.items():
                status_icon = "+" if result.success else "-"
                with st.expander(f"{'✅' if result.success else '❌'} {name}", expanded=not result.success):
                    st.code(result.stdout if result.success else result.stderr, language="text")

        if st.session_state.debug_results and st.button("Analyze with AI", type="secondary"):
            with st.spinner("AI is analyzing diagnostics..."):
                analysis = analyze_diagnostics(
                    st.session_state.debug_results,
                    profile=profile,
                )
                st.markdown(analysis)

    # ── Category Scan ─────────────────────────────────────────────────────
    with tab_category:
        st.markdown("### Category-Based Diagnostics")
        category = st.selectbox("Select Category", options=list(CATEGORY_MAP.keys()))

        if st.button("Run Category Scan", type="primary", key="cat_scan"):
            with st.spinner(f"Running {category} diagnostics..."):
                results = run_category_diagnostics(cp_node, category)

            for name, result in results.items():
                with st.expander(f"{'✅' if result.success else '❌'} {name}"):
                    st.code(result.stdout if result.success else result.stderr, language="text")

            if st.button("Analyze Category with AI", key="cat_ai"):
                with st.spinner("Analyzing..."):
                    analysis = analyze_diagnostics(results, profile=profile)
                    st.markdown(analysis)

    # ── Custom Command ────────────────────────────────────────────────────
    with tab_custom:
        st.markdown("### Run Custom Command")
        st.warning("Commands execute on the control-plane node via SSH.")
        custom_cmd = st.text_area(
            "Command",
            placeholder="kubectl get pods -A -o wide",
            height=100,
        )
        if st.button("Execute", type="primary", key="exec_custom") and custom_cmd:
            with st.spinner("Executing..."):
                result = run_custom_command(cp_node, custom_cmd)
                if result.success:
                    st.code(result.stdout, language="text")
                else:
                    st.error("Command failed")
                    st.code(result.stderr, language="text")

    # ── AI Debug Assistant ────────────────────────────────────────────────
    with tab_ai:
        st.markdown("### AI Debug Assistant")
        st.markdown("Describe your issue and get AI-powered debugging help.")

        issue = st.text_area(
            "Describe the issue",
            placeholder="e.g., Pods are stuck in CrashLoopBackOff in the default namespace",
            height=120,
        )

        col1, col2 = st.columns(2)
        with col1:
            auto_collect = st.checkbox("Auto-collect relevant diagnostics", value=True)
        with col2:
            check_pods = st.checkbox("Check for problematic pods", value=True)

        if st.button("Debug", type="primary", key="ai_debug") and issue:
            collected_data = ""

            if check_pods:
                with st.spinner("Checking pod issues..."):
                    pod_result = check_pod_issues(cp_node)
                    if pod_result.success and pod_result.stdout.strip():
                        collected_data += f"\n\nProblematic Pods:\n{pod_result.stdout}"
                        with st.expander("Problematic Pods"):
                            st.code(pod_result.stdout, language="text")

            if auto_collect:
                with st.spinner("Collecting diagnostics..."):
                    diag_results = run_category_diagnostics(cp_node, "Cluster Overview")
                    for name, result in diag_results.items():
                        if result.success:
                            collected_data += f"\n\n{name}:\n{result.stdout}"

            with st.spinner("AI is analyzing the issue..."):
                full_context = f"Issue: {issue}\n\nCollected Data:{collected_data}"
                suggestion = get_debug_suggestion(issue, collected_data)
                st.markdown("### AI Recommendation")
                st.markdown(suggestion)


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Monitoring Setup
# ══════════════════════════════════════════════════════════════════════════

def page_monitoring_setup():
    st.markdown("## Monitoring Setup")
    st.markdown("Deploy Prometheus, Grafana, dashboards, and alerting rules.")

    profile = _get_active_profile()
    if not profile:
        return

    cp_nodes = profile.get_control_plane_nodes()
    if not cp_nodes:
        st.error("No control-plane node defined in this profile.")
        return
    cp_node = cp_nodes[0]

    namespace = st.text_input("Monitoring Namespace", value="monitoring")

    tab_install, tab_dashboards, tab_alerts, tab_status, tab_scripts, tab_advice = st.tabs([
        "Install Stack",
        "Dashboards",
        "Alert Rules",
        "Status",
        "View Scripts",
        "AI Advice",
    ])

    # ── Install ───────────────────────────────────────────────────────────
    with tab_install:
        st.markdown("### Install Monitoring Stack")
        st.markdown("This installs **kube-prometheus-stack** (Prometheus + Grafana + exporters).")

        col1, col2 = st.columns(2)
        with col1:
            install_helm_first = st.checkbox("Install Helm (if not present)", value=True)
        with col2:
            install_alerts_too = st.checkbox("Also install alert rules", value=True)

        if st.button("Install Prometheus + Grafana", type="primary", use_container_width=True):
            if install_helm_first:
                with st.status("Installing Helm...", expanded=True):
                    result = install_helm(cp_node)
                    if result.success:
                        st.success("Helm ready!")
                    else:
                        st.error("Helm installation failed")
                        st.code(result.stderr, language="text")

            with st.status("Installing kube-prometheus-stack (this may take several minutes)...", expanded=True):
                result = install_prometheus_stack(cp_node, namespace)
                if result.success:
                    st.success("Prometheus + Grafana installed!")
                    st.code(result.stdout[-2000:], language="text")
                else:
                    st.error("Installation failed")
                    st.code(result.stderr, language="text")

            if install_alerts_too:
                with st.status("Installing alert rules...", expanded=True):
                    result = install_alert_rules(cp_node, namespace)
                    if result.success:
                        st.success("Alert rules installed!")
                    else:
                        st.error("Alert rules installation failed")
                        st.code(result.stderr, language="text")

    # ── Dashboards ────────────────────────────────────────────────────────
    with tab_dashboards:
        st.markdown("### Grafana Dashboards")
        st.markdown("Select dashboards to import into Grafana.")

        selected_dashboards = []
        cols = st.columns(2)
        for i, (key, dash) in enumerate(GRAFANA_DASHBOARDS.items()):
            with cols[i % 2]:
                if st.checkbox(f"**{dash['name']}**\n{dash['description']}", value=True, key=f"dash_{key}"):
                    selected_dashboards.append(key)

        if st.button("Import Dashboards", type="primary") and selected_dashboards:
            with st.status("Importing dashboards...", expanded=True):
                result = install_dashboards(cp_node, selected_dashboards, namespace)
                if result.success:
                    st.success(f"Imported {len(selected_dashboards)} dashboards!")
                    st.code(result.stdout, language="text")
                else:
                    st.error("Dashboard import failed")
                    st.code(result.stderr, language="text")

    # ── Alert Rules ───────────────────────────────────────────────────────
    with tab_alerts:
        st.markdown("### Alerting Rules")
        st.markdown("Install production-ready alerting rules for nodes, pods, and etcd.")

        with st.expander("View Alert Rules", expanded=False):
            st.code(generate_alerting_rules_script(namespace), language="yaml")

        if st.button("Install Alert Rules", type="primary", key="install_alerts"):
            with st.spinner("Installing alert rules..."):
                result = install_alert_rules(cp_node, namespace)
                if result.success:
                    st.success("Alert rules installed!")
                    st.code(result.stdout, language="text")
                else:
                    st.error("Failed to install alert rules")
                    st.code(result.stderr, language="text")

    # ── Status ────────────────────────────────────────────────────────────
    with tab_status:
        st.markdown("### Monitoring Stack Status")
        if st.button("Check Status", type="primary", key="mon_status"):
            with st.spinner("Checking monitoring stack..."):
                result = get_monitoring_status(cp_node, namespace)
                if result.success:
                    st.code(result.stdout, language="text")
                else:
                    st.warning("Could not retrieve monitoring status")
                    st.code(result.stderr, language="text")

    # ── View Scripts ──────────────────────────────────────────────────────
    with tab_scripts:
        st.markdown("### Generated Scripts")
        with st.expander("Prometheus Install Script"):
            st.code(generate_prometheus_install_script(namespace), language="bash")
        with st.expander("Dashboard Import Script"):
            all_keys = list(GRAFANA_DASHBOARDS.keys())
            st.code(generate_dashboard_import_script(all_keys, namespace), language="bash")
        with st.expander("Alert Rules Script"):
            st.code(generate_alerting_rules_script(namespace), language="bash")

    # ── AI Advice ─────────────────────────────────────────────────────────
    with tab_advice:
        st.markdown("### AI Monitoring Advisor")
        if st.button("Get Monitoring Recommendations", type="primary", key="mon_advice"):
            current_status = ""
            status_result = get_monitoring_status(cp_node, namespace)
            if status_result.success:
                current_status = status_result.stdout

            with st.spinner("Getting AI recommendations..."):
                advice = get_monitoring_advice(profile, current_status)
                st.markdown(advice)


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Log Analysis
# ══════════════════════════════════════════════════════════════════════════

def page_log_analysis():
    st.markdown("## Log Analysis & Error Correlation")
    st.markdown("Collect, parse, and analyze logs from your cluster components.")

    profile = _get_active_profile()
    if not profile:
        return

    cp_nodes = profile.get_control_plane_nodes()
    if not cp_nodes:
        st.error("No control-plane node defined in this profile.")
        return
    cp_node = cp_nodes[0]

    tab_system, tab_pod, tab_correlation, tab_ai = st.tabs([
        "System Logs",
        "Pod Logs",
        "Error Correlation",
        "AI Log Analysis",
    ])

    # ── System Logs ───────────────────────────────────────────────────────
    with tab_system:
        st.markdown("### System Component Logs")
        col1, col2, col3 = st.columns(3)
        with col1:
            sources = st.multiselect(
                "Log Sources",
                options=list(LOG_SOURCES.keys()),
                default=["Kubelet", "CRI-O", "Events"],
            )
        with col2:
            log_lines = st.number_input("Lines to fetch", min_value=50, max_value=1000, value=200)
        with col3:
            since_options = {"Last 15 min": ("15 minutes ago", "15m"),
                             "Last 1 hour": ("1 hour ago", "1h"),
                             "Last 6 hours": ("6 hours ago", "6h"),
                             "Last 24 hours": ("24 hours ago", "24h")}
            since_label = st.selectbox("Time Range", options=list(since_options.keys()), index=1)
            since, since_k8s = since_options[since_label]

        if st.button("Collect Logs", type="primary", key="collect_sys"):
            log_data = {}
            for source in sources:
                with st.spinner(f"Collecting {source} logs..."):
                    result = collect_logs(cp_node, source, log_lines, since, since_k8s)
                    if result.success:
                        log_data[source] = result.stdout
                        analysis = analyze_logs(result.stdout, source)

                        with st.expander(
                            f"{'❌' if analysis.error_count > 0 else '✅'} {source} "
                            f"({analysis.error_count} errors, {analysis.warning_count} warnings)",
                            expanded=analysis.error_count > 0,
                        ):
                            # Metrics
                            m1, m2, m3 = st.columns(3)
                            m1.metric("Total Lines", analysis.total_lines)
                            m2.metric("Errors", analysis.error_count)
                            m3.metric("Warnings", analysis.warning_count)

                            if analysis.error_patterns:
                                st.markdown("**Top Error Patterns:**")
                                for pattern, count in list(analysis.error_patterns.items())[:5]:
                                    st.markdown(f"- `{pattern}` (x{count})")

                            st.code(result.stdout[-3000:], language="text")
                    else:
                        with st.expander(f"❌ {source} — FAILED"):
                            st.code(result.stderr, language="text")

            st.session_state.log_analysis_results = log_data

    # ── Pod Logs ──────────────────────────────────────────────────────────
    with tab_pod:
        st.markdown("### Pod Logs")
        col1, col2 = st.columns(2)
        with col1:
            pod_ns = st.text_input("Namespace", value="default", key="pod_ns")
            pod_name = st.text_input("Pod Name", placeholder="my-pod-xyz", key="pod_name_input")
        with col2:
            container = st.text_input("Container (optional)", key="pod_container")
            pod_lines = st.number_input("Lines", min_value=50, max_value=1000, value=200, key="pod_lines")
            pod_previous = st.checkbox("Previous container logs (crash recovery)")

        if st.button("Fetch Pod Logs", type="primary", key="fetch_pod") and pod_name:
            with st.spinner(f"Fetching logs for {pod_ns}/{pod_name}..."):
                result = collect_pod_logs(
                    cp_node, pod_ns, pod_name, container, pod_lines,
                    "1h", pod_previous,
                )
                if result.success:
                    analysis = analyze_logs(result.stdout, f"{pod_ns}/{pod_name}")
                    m1, m2, m3 = st.columns(3)
                    m1.metric("Total Lines", analysis.total_lines)
                    m2.metric("Errors", analysis.error_count)
                    m3.metric("Warnings", analysis.warning_count)

                    if analysis.error_patterns:
                        st.markdown("**Error Patterns:**")
                        for pattern, count in list(analysis.error_patterns.items())[:10]:
                            st.markdown(f"- `{pattern}` (x{count})")

                    st.code(result.stdout[-5000:], language="text")

                    if analysis.error_count > 0 and st.button("Analyze with AI", key="pod_ai"):
                        with st.spinner("AI analyzing pod logs..."):
                            ai_analysis = llm_analyze_logs(
                                result.stdout, f"{pod_ns}/{pod_name}"
                            )
                            st.markdown(ai_analysis)
                else:
                    st.error("Failed to fetch pod logs")
                    st.code(result.stderr, language="text")

    # ── Error Correlation ─────────────────────────────────────────────────
    with tab_correlation:
        st.markdown("### Cross-Source Error Correlation")
        st.markdown("Collect logs from multiple sources and correlate errors across them.")

        corr_sources = st.multiselect(
            "Sources to correlate",
            options=list(LOG_SOURCES.keys()),
            default=["Kubelet", "CRI-O", "API Server", "Events"],
            key="corr_sources",
        )

        if st.button("Collect & Correlate", type="primary", key="correlate"):
            with st.spinner("Collecting logs from multiple sources..."):
                results = collect_multi_source_logs(cp_node, corr_sources, lines=150)

            correlated = correlate_errors(results)

            if correlated:
                st.markdown(f"### Found {len(correlated)} correlated error groups")
                for i, group in enumerate(correlated):
                    with st.expander(
                        f"Correlation #{i + 1}: {', '.join(group['sources_involved'])}",
                        expanded=True,
                    ):
                        st.markdown(f"**Primary Error** ({group['primary']['source']}):")
                        st.code(group["primary"]["message"], language="text")
                        st.markdown("**Related Errors:**")
                        for related in group["related"]:
                            st.markdown(f"- **{related['source']}**: `{related['message'][:200]}`")
            else:
                st.info("No correlated errors found across sources.")

            # LLM correlation analysis
            if st.button("Deep AI Correlation Analysis", key="deep_corr"):
                multi_logs = {
                    src: res.stdout for src, res in results.items() if res.success
                }
                with st.spinner("AI is performing deep correlation analysis..."):
                    analysis = llm_correlate_analysis(multi_logs)
                    st.markdown(analysis)

    # ── AI Log Analysis ───────────────────────────────────────────────────
    with tab_ai:
        st.markdown("### AI-Powered Log Analysis")
        st.markdown("Paste logs or describe an issue for AI analysis.")

        log_input = st.text_area(
            "Paste log output",
            height=200,
            placeholder="Paste your Kubernetes logs here...",
        )
        context_input = st.text_input(
            "Additional context",
            placeholder="e.g., This started happening after we upgraded to K8s 1.30",
        )

        if st.button("Analyze Logs", type="primary", key="ai_log_analyze") and log_input:
            with st.spinner("AI is analyzing logs..."):
                analysis = llm_analyze_logs(log_input, context=context_input)
                st.markdown(analysis)


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: AI Assistant
# ══════════════════════════════════════════════════════════════════════════

def page_ai_assistant():
    st.markdown("## AI Kubernetes Assistant")
    st.markdown("Chat with the AI about any Kubernetes topic.")

    # Chat history
    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])

    # Chat input
    if prompt := st.chat_input("Ask about Kubernetes..."):
        st.session_state.chat_history.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.markdown(prompt)

        with st.chat_message("assistant"):
            placeholder = st.empty()
            full_response = ""
            for chunk in stream_llm(
                prompt,
                conversation_history=st.session_state.chat_history[:-1],
            ):
                full_response += chunk
                placeholder.markdown(full_response + "▌")
            placeholder.markdown(full_response)

        st.session_state.chat_history.append({"role": "assistant", "content": full_response})


# ── Helper functions ──────────────────────────────────────────────────────

def _get_active_profile() -> ClusterProfile | None:
    """Get the active profile or show a warning."""
    if not st.session_state.active_profile:
        st.warning("No active cluster profile selected. Please create or select one in the Profile Manager.")
        return None
    profile = load_profile(st.session_state.active_profile)
    if not profile:
        st.error(f"Profile '{st.session_state.active_profile}' not found.")
        return None
    return profile


def _show_profile_summary(profile: ClusterProfile):
    """Display a compact profile summary."""
    cols = st.columns(5)
    cols[0].metric("Profile", profile.name)
    cols[1].metric("K8s Version", profile.kubernetes_version)
    cols[2].metric("Runtime", f"CRI-O {profile.crio_version}")
    cols[3].metric("CNI", "Flannel")
    cols[4].metric("Nodes", f"{len(profile.get_control_plane_nodes())} CP + {len(profile.get_worker_nodes())} W")


# ── Main Router ───────────────────────────────────────────────────────────

def main():
    page = render_sidebar()

    if page == "Profile Manager":
        page_profile_manager()
    elif page == "Cluster Creation":
        page_cluster_creation()
    elif page == "Cluster Debugger":
        page_cluster_debugger()
    elif page == "Monitoring Setup":
        page_monitoring_setup()
    elif page == "Log Analysis":
        page_log_analysis()
    elif page == "AI Assistant":
        page_ai_assistant()


if __name__ == "__main__":
    main()
