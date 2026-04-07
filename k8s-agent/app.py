"""K8s Agent — Streamlit-based Kubernetes Cluster Management UI."""

import sys
import os

# Ensure the k8s-agent directory is on the Python path so sibling imports work.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import json
import streamlit as st
# Navigation uses native st.radio — no third-party component needed.

import config
from config import is_llm_configured, get_kubectl_path, fetch_namespaces, get_kubeconfig_path
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
    run_ssh_command,
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
    upload_flannel_manifest_to_node,
    run_kubectl,
    ProvisionStep,
    _run_step,
    get_common_setup_steps,
    get_control_plane_steps,
    get_worker_join_steps,
    get_best_practices_steps,
    get_cluster_reset_steps,
)
from modules.cluster_debugger import (
    DIAGNOSTIC_COMMANDS,
    KUBECTL_DIAGNOSTIC_COMMANDS,
    CATEGORY_MAP,
    get_available_commands,
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
    get_available_log_sources,
    collect_logs,
    collect_pod_logs,
    collect_multi_source_logs,
    analyze_logs,
    correlate_errors,
    llm_analyze_logs,
    llm_correlate_analysis,
    get_pod_list,
    smart_analyze,
    cluster_logs,
    detect_anomalies,
    mine_log_patterns,
    summarize_logs,
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
        "_flash_message": None,
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
                    if profile.cluster_source == "imported":
                        st.caption(
                            f"K8s {profile.kubernetes_version} | Imported Cluster"
                        )
                    else:
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
        st.markdown("### Navigation")
        nav_options = [
            "Multi-Cluster Dashboard",
            "Profile Manager",
            "Cluster Creation",
            "Resource Viewer",
            "Cluster Debugger",
            "Monitoring Setup",
            "Log Analysis",
            "Upgrade Planner",
            "Certificate Manager",
            "Cost Optimizer",
            "AI Assistant",
        ]
        selected_page = st.radio(
            "Go to",
            options=nav_options,
            index=0,
            label_visibility="collapsed",
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

    # Show any flash message from a previous action (e.g. after st.rerun)
    if st.session_state.get("_flash_message"):
        _flash = st.session_state._flash_message
        if _flash[0] == "success":
            st.success(_flash[1])
        elif _flash[0] == "error":
            st.error(_flash[1])
        elif _flash[0] == "info":
            st.info(_flash[1])
        st.session_state._flash_message = None

    tab_create, tab_import_cluster, tab_list, tab_import = st.tabs([
        "Create Profile", "Import Existing Cluster", "Manage Profiles", "Import / Export",
    ])

    # ── Create Profile ────────────────────────────────────────────────────
    with tab_create:
        with st.form("create_profile_form"):
            st.markdown("### New Cluster Profile")
            col1, col2 = st.columns(2)

            with col1:
                name = st.text_input("Profile Name *", placeholder="production-cluster")
                description = st.text_area("Description", placeholder="Production on-prem cluster")
                k8s_version = st.selectbox(
                    "Kubernetes Version",
                    ["1.35", "1.34", "1.33", "1.32", "1.31", "1.30", "1.29", "1.28", "1.27"],
                    index=0,
                )
                crio_version = st.selectbox(
                    "CRI-O Version",
                    ["1.35", "1.34", "1.33", "1.32", "1.31", "1.30", "1.29", "1.28", "1.27"],
                    index=0,
                )
                pod_security = st.selectbox(
                    "Pod Security Standard",
                    ["restricted", "baseline", "privileged"],
                    index=0,
                    help="Controls what pods are allowed to run in the cluster.",
                )
                # Explain each PSS level
                with st.expander("What do these Pod Security Standards mean?"):
                    st.markdown(
                        "**Restricted** (most secure)\n"
                        "- Heavily restricted policy following Pod hardening best practices.\n"
                        "- Disallows privilege escalation, host namespaces, host paths, and most Linux capabilities.\n"
                        "- Containers must run as non-root with a read-only root filesystem.\n"
                        "- Only allows seccomp profile RuntimeDefault or Localhost.\n"
                        "- Best for: production workloads, multi-tenant clusters, security-sensitive environments.\n\n"
                        "**Baseline** (moderate)\n"
                        "- Minimally restrictive policy that prevents known privilege escalations.\n"
                        "- Allows most default Kubernetes configurations but blocks hostNetwork, hostPID, hostIPC.\n"
                        "- Containers can run as root but cannot use privileged mode.\n"
                        "- Allows all seccomp profiles.\n"
                        "- Best for: general workloads, development/staging, teams new to PSS.\n\n"
                        "**Privileged** (unrestricted)\n"
                        "- Completely unrestricted policy — no security restrictions enforced.\n"
                        "- Allows privileged containers, host namespaces, host paths, any capabilities.\n"
                        "- Containers can run as root with full access to the host.\n"
                        "- Best for: system-level workloads (monitoring agents, CNI plugins, storage drivers), "
                        "trusted single-tenant clusters.\n\n"
                        "**Recommendation:** Start with *Restricted* and relax to *Baseline* only for "
                        "workloads that require it. Avoid *Privileged* unless absolutely necessary."
                    )

            with col2:
                pod_cidr = st.text_input("Pod CIDR", value="10.244.0.0/16")
                service_cidr = st.text_input("Service CIDR", value="10.96.0.0/12")
                dns_domain = st.text_input("DNS Domain", value="cluster.local")

            st.divider()
            st.markdown("### Storage Paths")
            st.markdown(
                "Configure where CRI-O stores container images, pods, and logs. "
                "Change these to use a dedicated disk instead of the default `/var/lib`."
            )
            scol1, scol2 = st.columns(2)
            with scol1:
                crio_root = st.text_input(
                    "CRI-O Storage Root",
                    value="/var/lib/containers/storage",
                    help="Root directory for CRI-O container/image storage (default: /var/lib/containers/storage)",
                )
                crio_runroot = st.text_input(
                    "CRI-O Run Root",
                    value="/run/containers/storage",
                    help="Runtime root for CRI-O (default: /run/containers/storage)",
                )
            with scol2:
                kubelet_root = st.text_input(
                    "Kubelet Data Directory",
                    value="/var/lib/kubelet",
                    help="Kubelet data directory for pods, volumes, etc. (default: /var/lib/kubelet)",
                )
                log_root = st.text_input(
                    "Log Root Directory",
                    value="/var/log",
                    help="Base directory for all logs — CRI-O pod logs, kubernetes audit logs, etc. (default: /var/log)",
                )

            st.divider()
            st.markdown("### Proxy Settings (Master Node)")
            st.markdown(
                "Configure HTTP/HTTPS proxy for the master/control-plane node. "
                "These are used during package installation and cluster initialization."
            )
            pcol1, pcol2 = st.columns(2)
            with pcol1:
                http_proxy = st.text_input(
                    "HTTP Proxy",
                    value="",
                    placeholder="http://proxy.example.com:8080",
                    help="Primary HTTP proxy for outbound connections",
                )
                https_proxy = st.text_input(
                    "HTTPS Proxy",
                    value="",
                    placeholder="http://proxy.example.com:8443",
                    help="Primary HTTPS proxy for outbound connections",
                )
                no_proxy = st.text_input(
                    "No Proxy",
                    value="",
                    placeholder="localhost,127.0.0.1,10.96.0.0/12,10.244.0.0/16",
                    help="Comma-separated list of hosts/CIDRs to bypass proxy",
                )
            with pcol2:
                http_proxy_alt = st.text_input(
                    "Alternate HTTP Proxy",
                    value="",
                    placeholder="http://backup-proxy.example.com:8080",
                    help="Fallback HTTP proxy if the primary is unavailable",
                )
                https_proxy_alt = st.text_input(
                    "Alternate HTTPS Proxy",
                    value="",
                    placeholder="http://backup-proxy.example.com:8443",
                    help="Fallback HTTPS proxy if the primary is unavailable",
                )

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
                        crio_root=crio_root,
                        crio_runroot=crio_runroot,
                        kubelet_root=kubelet_root,
                        log_root=log_root,
                        http_proxy=http_proxy,
                        https_proxy=https_proxy,
                        no_proxy=no_proxy,
                        http_proxy_alt=http_proxy_alt,
                        https_proxy_alt=https_proxy_alt,
                    )
                    path = save_profile(profile)
                    st.session_state.active_profile = name
                    st.session_state._flash_message = ("success", f"Profile '{name}' created successfully! Select it from the sidebar to get started.")
                    st.rerun()

    # ── Import Existing Cluster ──────────────────────────────────────────
    with tab_import_cluster:
        st.markdown("### Import Existing Kubernetes Cluster")
        st.markdown(
            "Connect to an existing K8s cluster by uploading its **kubeconfig** file. "
            "This lets you use the Debugger, Monitoring, Log Analysis, and Resource Viewer "
            "without provisioning a new cluster."
        )

        # NOTE: file_uploader is kept OUTSIDE st.form because Streamlit
        # resets the uploaded file on form submission, causing the import
        # to silently do nothing.
        import_name = st.text_input(
            "Profile Name *",
            placeholder="my-existing-cluster",
            key="import_cluster_name",
        )
        import_desc = st.text_area(
            "Description",
            placeholder="Production cluster running in datacenter A",
            key="import_cluster_desc",
        )
        kubeconfig_file = st.file_uploader(
            "Upload kubeconfig file",
            type=["yaml", "yml", "conf", "config", "txt"],
            key="kubeconfig_upload",
            help="Usually found at ~/.kube/config on your cluster's control-plane node. "
                 "If your file has no extension, rename it to config.yaml or config.txt before uploading.",
        )
        k8s_ver = st.text_input(
            "Kubernetes Version (optional)",
            placeholder="1.30",
            value="1.30",
            key="import_cluster_k8s_ver",
        )

        if st.button("Import Cluster", type="primary", use_container_width=True, key="import_cluster_btn"):
            if not import_name:
                st.error("Profile name is required.")
            elif not kubeconfig_file:
                st.error("Please upload a kubeconfig file.")
            else:
                kubeconfig_content = kubeconfig_file.read().decode("utf-8")
                profile = ClusterProfile(
                    name=import_name,
                    description=import_desc,
                    kubernetes_version=k8s_ver or "1.30",
                    status="imported",
                    cluster_source="imported",
                    kubeconfig_content=kubeconfig_content,
                )
                try:
                    save_profile(profile)
                    st.session_state.active_profile = import_name
                    st.session_state._flash_message = (
                        "success",
                        f"Cluster '{import_name}' imported successfully! "
                        "It is now the active profile. Use the sidebar navigation to go to "
                        "Cluster Debugger, Resource Viewer, Monitoring Setup, etc."
                    )
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to import cluster: {e}")

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
                    st.markdown(f"**CRI-O Root:** `{profile.crio_root}` | **Kubelet Dir:** `{profile.kubelet_root}`")
                    st.markdown(f"**Log Root:** `{profile.log_root}`")
                    if profile.http_proxy or profile.https_proxy:
                        st.markdown(f"**Proxy:** `{profile.http_proxy or profile.https_proxy}`")
                    if profile.http_proxy_alt or profile.https_proxy_alt:
                        st.markdown(f"**Alt Proxy:** `{profile.http_proxy_alt or profile.https_proxy_alt}`")
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

    tab_preflight, tab_provision, tab_reset, tab_scripts, tab_manifests, tab_advice = st.tabs([
        "Pre-flight Checks",
        "Provision Cluster",
        "Reset Cluster",
        "View Scripts",
        "Offline Manifests",
        "AI Advice",
    ])

    # ── Pre-flight: SSH connectivity ──────────────────────────────────────
    with tab_preflight:
        st.markdown("### SSH Connectivity Test")
        st.markdown("Test SSH access to all nodes before provisioning.")

        if profile.cluster_source == "imported":
            st.info(
                "SSH connectivity tests are not applicable for imported clusters. "
                "Imported clusters connect via kubeconfig — no SSH access is needed. "
                "Use the **Cluster Debugger** or **Resource Viewer** to verify connectivity."
            )
        elif not profile.nodes:
            st.warning("No nodes defined in this profile. Add nodes in the Profile Manager first.")
        else:
            if st.button("Test All Nodes", type="primary"):
                all_ok = True
                for node in profile.nodes:
                    with st.status(f"Testing {node.get('hostname', node['ip_address'])}...", expanded=True):
                        result = test_ssh_connectivity(node)
                        if result.success:
                            st.success(f"Connected to {node['ip_address']}")
                            st.code(result.stdout, language="text")
                        else:
                            all_ok = False
                            st.error(f"Failed to connect to {node['ip_address']}")
                            st.code(result.stderr, language="text")
                if all_ok:
                    st.success("All nodes are reachable via SSH. You can proceed to provisioning.")
                else:
                    st.error("Some nodes failed SSH connectivity. Fix the issues above before provisioning.")

    # ── Provision ─────────────────────────────────────────────────────────
    with tab_provision:
        st.markdown("### Automated Cluster Provisioning")

        if profile.cluster_source == "imported":
            st.info(
                "Provisioning is not available for imported clusters. "
                "This cluster was imported via kubeconfig and is managed externally. "
                "Use the **Resource Viewer**, **Cluster Debugger**, or **Monitoring Setup** "
                "pages to work with your cluster."
            )
        elif not profile.nodes:
            st.warning("No nodes defined in this profile. Add nodes in the Profile Manager first.")
        else:
            st.warning(
                "This will SSH into each node and execute every provisioning step "
                "automatically. Ensure all nodes are accessible and you have root/sudo access."
            )

        cp_nodes = profile.get_control_plane_nodes()
        worker_nodes = profile.get_worker_nodes()

        if profile.cluster_source != "imported" and profile.nodes:
            st.markdown(f"**Control Plane:** {len(cp_nodes)} node(s) | **Workers:** {len(worker_nodes)} node(s)")

            col1, col2, col3 = st.columns(3)
            with col1:
                step1 = st.checkbox("Step 1: Common Setup (all nodes)", value=True)
            with col2:
                step2 = st.checkbox("Step 2: Init Control Plane", value=True)
            with col3:
                step3 = st.checkbox("Step 3: Join Workers", value=True)
            step4 = st.checkbox("Step 4: Apply Best Practices", value=True)
        else:
            step1 = step2 = step3 = step4 = False

        if profile.cluster_source == "imported" or not profile.nodes:
            pass  # messages shown above
        elif st.button("Start Provisioning", type="primary", use_container_width=True):
            update_profile_status(profile.name, "provisioning")
            overall_success = True

            # ── Step 1: Common setup on ALL nodes (granular per-step) ────
            if step1:
                st.markdown("---")
                st.markdown("### Step 1: Common Node Setup")
                common_steps = get_common_setup_steps(profile)
                for node in profile.nodes:
                    node_label = f"{node.get('hostname', node['ip_address'])} ({node['role']})"
                    st.markdown(f"#### Node: {node_label}")
                    node_ok = True
                    progress = st.progress(0, text=f"Starting setup on {node_label}...")
                    for idx, step in enumerate(common_steps):
                        pct = int((idx / len(common_steps)) * 100)
                        progress.progress(pct, text=f"[{idx+1}/{len(common_steps)}] {step.title}")
                        with st.status(f"{step.title}...", expanded=False) as status:
                            result = _run_step(node, step)
                            if result.success:
                                st.code(result.stdout[-1500:] if result.stdout else "(no output)", language="text")
                                status.update(label=f"{step.title} — done", state="complete")
                            else:
                                st.error(f"FAILED: {step.title}")
                                st.code(result.stderr or result.stdout, language="text")
                                status.update(label=f"{step.title} — FAILED", state="error")
                                node_ok = False
                                if step.fatal:
                                    overall_success = False
                                    break
                    progress.progress(100, text=f"{'Setup complete' if node_ok else 'Setup FAILED'} on {node_label}")
                    if node_ok:
                        st.success(f"Common setup complete on {node['ip_address']}")
                    else:
                        st.error(f"Common setup failed on {node['ip_address']}")

            # ── Step 2: Control plane init (granular per-step) ───────────
            if step2 and cp_nodes and overall_success:
                st.markdown("---")
                st.markdown("### Step 2: Control Plane Initialization")
                cp_node = cp_nodes[0]
                cp_steps = get_control_plane_steps(profile)
                progress = st.progress(0, text="Starting control plane init...")
                for idx, step in enumerate(cp_steps):
                    pct = int((idx / len(cp_steps)) * 100)
                    progress.progress(pct, text=f"[{idx+1}/{len(cp_steps)}] {step.title}")
                    with st.status(f"{step.title}...", expanded=False) as status:
                        result = _run_step(cp_node, step)
                        if result.success:
                            st.code(result.stdout[-2000:] if result.stdout else "(no output)", language="text")
                            status.update(label=f"{step.title} — done", state="complete")
                        else:
                            st.error(f"FAILED: {step.title}")
                            st.code(result.stderr or result.stdout, language="text")
                            status.update(label=f"{step.title} — FAILED", state="error")
                            overall_success = False
                            if step.fatal:
                                break
                progress.progress(100, text="Control plane initialization complete" if overall_success else "Control plane init FAILED")
                if overall_success:
                    st.success("Control plane initialized!")
                else:
                    st.error("Control plane initialization failed!")

            # ── Step 3: Join workers (granular per-step) ─────────────────
            if step3 and worker_nodes and cp_nodes and overall_success:
                st.markdown("---")
                st.markdown("### Step 3: Join Worker Nodes")
                join_cmd = retrieve_join_command(cp_nodes[0])
                if join_cmd:
                    worker_join_steps = get_worker_join_steps(join_cmd)
                    for node in worker_nodes:
                        node_label = f"{node.get('hostname', node['ip_address'])}"
                        st.markdown(f"#### Worker: {node_label}")
                        for step in worker_join_steps:
                            with st.status(f"{step.title} on {node_label}...", expanded=False) as status:
                                result = _run_step(node, step)
                                if result.success:
                                    st.code(result.stdout[-1500:] if result.stdout else "(no output)", language="text")
                                    status.update(label=f"{step.title} — done", state="complete")
                                    st.success(f"Worker {node['ip_address']} joined!")
                                else:
                                    st.error(f"FAILED to join {node['ip_address']}")
                                    st.code(result.stderr or result.stdout, language="text")
                                    status.update(label=f"{step.title} — FAILED", state="error")
                else:
                    st.error("Could not retrieve join command from control plane.")

            # ── Step 4: Best practices (granular per-step) ───────────────
            if step4 and cp_nodes and overall_success:
                st.markdown("---")
                st.markdown("### Step 4: Apply Best Practices")
                bp_steps = get_best_practices_steps()
                progress = st.progress(0, text="Applying best practices...")
                for idx, step in enumerate(bp_steps):
                    pct = int((idx / len(bp_steps)) * 100)
                    progress.progress(pct, text=f"[{idx+1}/{len(bp_steps)}] {step.title}")
                    with st.status(f"{step.title}...", expanded=False) as status:
                        result = _run_step(cp_nodes[0], step)
                        if result.success:
                            st.code(result.stdout[-1000:] if result.stdout else "(no output)", language="text")
                            status.update(label=f"{step.title} — done", state="complete")
                        else:
                            st.error(f"FAILED: {step.title}")
                            st.code(result.stderr or result.stdout, language="text")
                            status.update(label=f"{step.title} — FAILED", state="error")
                            if step.fatal:
                                overall_success = False
                                break
                progress.progress(100, text="Best practices applied" if overall_success else "Best practices FAILED")
                if overall_success:
                    st.success("Best practices applied!")

            # ── Final cluster status ─────────────────────────────────────
            st.markdown("---")
            st.markdown("### Cluster Status")
            if cp_nodes and overall_success:
                with st.status("Checking cluster status...", expanded=True) as status:
                    result = get_cluster_status(cp_nodes[0])
                    if result.success:
                        update_profile_status(profile.name, "active")
                        st.success("Cluster is active!")
                        st.code(result.stdout, language="text")
                        status.update(label="Cluster is active", state="complete")
                    else:
                        update_profile_status(profile.name, "error")
                        st.error("Could not verify cluster status")
                        st.code(result.stderr, language="text")
                        status.update(label="Status check failed", state="error")
            elif not overall_success:
                update_profile_status(profile.name, "error")
                st.error("Provisioning did not complete successfully. Check the errors above.")

    # ── Reset Cluster ────────────────────────────────────────────────────
    with tab_reset:
        st.markdown("### Reset / Tear Down Cluster")
        st.markdown(
            "Completely reset the Kubernetes cluster on all (or selected) nodes. "
            "This will run `kubeadm reset`, stop services, remove CRI-O data, "
            "CNI configs, etcd data, and flush iptables — preparing nodes for a "
            "fresh cluster installation."
        )

        if profile.cluster_source == "imported":
            st.info(
                "Cluster reset requires SSH access to each node and is only "
                "available for **provisioned** clusters. For imported clusters, "
                "run `kubeadm reset` directly on each node."
            )
        else:
            all_nodes = profile.nodes
            if not all_nodes:
                st.warning("No nodes defined in this profile.")
            else:
                st.error(
                    "**WARNING:** This is a destructive operation. All Kubernetes data, "
                    "containers, etcd data, and configuration will be permanently deleted "
                    "from the selected nodes. This cannot be undone."
                )

                # Node selection
                reset_node_labels = [
                    f"{n.get('hostname', n.get('ip_address', '?'))} ({n.get('ip_address', '?')}) [{n.get('role', '?')}]"
                    for n in all_nodes
                ]
                reset_all = st.checkbox("Reset ALL nodes", value=True, key="reset_all_nodes")

                if not reset_all:
                    reset_idx = st.multiselect(
                        "Select nodes to reset",
                        options=list(range(len(all_nodes))),
                        format_func=lambda i: reset_node_labels[i],
                        default=list(range(len(all_nodes))),
                        key="reset_node_select",
                    )
                    reset_nodes = [all_nodes[i] for i in reset_idx]
                else:
                    reset_nodes = all_nodes

                # Options
                col_r1, col_r2 = st.columns(2)
                with col_r1:
                    remove_packages = st.checkbox(
                        "Also remove kubeadm/kubelet/kubectl packages",
                        value=False,
                        key="reset_remove_pkgs",
                    )
                with col_r2:
                    auto_reprovision = st.checkbox(
                        "Re-provision cluster after reset",
                        value=False,
                        key="reset_reprovision",
                        help="After reset completes, automatically start fresh provisioning using the Provision Cluster flow.",
                    )

                # Confirmation
                confirm_text = st.text_input(
                    'Type **RESET** to confirm',
                    key="reset_confirm",
                    help="Type RESET (all caps) to enable the reset button.",
                )

                reset_enabled = confirm_text.strip() == "RESET" and len(reset_nodes) > 0
                if st.button(
                    f"Reset {len(reset_nodes)} Node(s)",
                    type="primary",
                    disabled=not reset_enabled,
                    use_container_width=True,
                    key="reset_go",
                ):
                    update_profile_status(profile.name, "provisioning")
                    reset_steps = get_cluster_reset_steps(profile)

                    # Optionally add package removal step
                    if remove_packages:
                        reset_steps.append(
                            ProvisionStep(
                                name="remove_packages",
                                title="Remove kubeadm/kubelet/kubectl packages",
                                script="""set -uo pipefail
echo '>> Removing Kubernetes packages...'
if command -v yum &>/dev/null; then
    yum remove -y kubeadm kubelet kubectl cri-o 2>/dev/null || true
elif command -v apt-get &>/dev/null; then
    apt-get remove -y --purge kubeadm kubelet kubectl cri-o 2>/dev/null || true
fi
echo 'Packages removed.'
""",
                                timeout=120,
                                fatal=False,
                            )
                        )

                    reset_success = True
                    for node in reset_nodes:
                        node_label = f"{node.get('hostname', node.get('ip_address', '?'))} ({node.get('ip_address', '')})"
                        st.markdown(f"---\n#### Resetting: {node_label} [{node.get('role', '')}]")
                        progress = st.progress(0, text=f"Starting reset on {node_label}...")
                        node_ok = True
                        for idx, step in enumerate(reset_steps):
                            pct = int((idx / len(reset_steps)) * 100)
                            progress.progress(pct, text=f"[{idx+1}/{len(reset_steps)}] {step.title}")
                            with st.status(f"{step.title}...", expanded=False) as status:
                                result = _run_step(node, step)
                                if result.success:
                                    st.code(result.stdout[-1500:] if result.stdout else "(no output)", language="text")
                                    status.update(label=f"{step.title} — done", state="complete")
                                else:
                                    st.warning(f"{step.title} — issue encountered")
                                    st.code(result.stderr or result.stdout, language="text")
                                    status.update(label=f"{step.title} — issue", state="error")
                                    node_ok = False
                                    if step.fatal:
                                        reset_success = False
                                        break
                        progress.progress(100, text=f"{'Reset complete' if node_ok else 'Reset had issues'} on {node_label}")
                        if node_ok:
                            st.success(f"Node {node_label} reset successfully.")
                        else:
                            st.warning(f"Node {node_label} reset completed with some issues. Check details above.")

                    if reset_success:
                        update_profile_status(profile.name, "draft")
                        st.success("All selected nodes have been reset. The cluster has been torn down.")
                        st.info("You can now go to the **Provision Cluster** tab to create a new cluster on these nodes.")

                        if auto_reprovision:
                            st.markdown("---")
                            st.markdown("### Auto Re-provisioning")
                            st.info(
                                "Auto re-provision is enabled. Please switch to the **Provision Cluster** tab "
                                "and click **Start Provisioning** to set up a fresh cluster with the current profile settings."
                            )
                    else:
                        update_profile_status(profile.name, "error")
                        st.error("Reset encountered fatal errors on some nodes. Review the output above before re-provisioning.")

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

    # ── Offline Manifests ───────────────────────────────────────────────────
    with tab_manifests:
        st.markdown("### Offline / Custom Manifests")
        st.markdown(
            "If your environment cannot download manifests directly (air-gapped / proxy-restricted), "
            "upload them here. They will be used instead of the default download URLs during provisioning."
        )

        st.markdown("#### Flannel CNI Manifest")
        flannel_file = st.file_uploader(
            "Upload kube-flannel.yml",
            type=["yml", "yaml"],
            key="flannel_upload",
            help="Download from: https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml",
        )
        if flannel_file is not None:
            flannel_path = os.path.join(config.UPLOADS_DIR, "kube-flannel.yml")
            with open(flannel_path, "wb") as f:
                f.write(flannel_file.getvalue())
            profile.flannel_manifest_path = flannel_path
            save_profile(profile)
            st.success(f"Flannel manifest saved. It will be SCP'd to nodes during provisioning.")

        if profile.flannel_manifest_path:
            st.info(f"Current Flannel manifest: `{profile.flannel_manifest_path}`")
            if st.button("Clear Flannel manifest (use default URL)", key="clear_flannel"):
                profile.flannel_manifest_path = ""
                save_profile(profile)
                st.rerun()
        else:
            st.info("No custom manifest — Flannel will be downloaded from the official GitHub release URL.")

        st.markdown("---")
        st.markdown("#### Other Manifests")
        st.markdown(
            "You can also upload any additional YAML manifests. They will be stored "
            "and can be applied manually via the **Custom Command** feature in the Cluster Debugger."
        )
        extra_file = st.file_uploader(
            "Upload additional manifest (YAML)",
            type=["yml", "yaml"],
            key="extra_manifest_upload",
        )
        if extra_file is not None:
            extra_path = os.path.join(config.UPLOADS_DIR, extra_file.name)
            with open(extra_path, "wb") as f:
                f.write(extra_file.getvalue())
            st.success(f"Saved `{extra_file.name}` to uploads.")

        # List existing uploaded files
        if os.path.exists(config.UPLOADS_DIR):
            uploaded_files = [
                f for f in os.listdir(config.UPLOADS_DIR)
                if f.endswith((".yml", ".yaml"))
            ]
            if uploaded_files:
                st.markdown("**Uploaded manifests:**")
                for fname in sorted(uploaded_files):
                    st.markdown(f"- `{fname}`")

    # ── AI Advice ─────────────────────────────────────────────────────────
    with tab_advice:
        st.markdown("### AI Cluster Setup Advisor")
        if not is_llm_configured():
            st.info(
                "LLM is not configured. Set `LLM_API_URL` and `LLM_API_KEY` "
                "environment variables to enable AI-powered recommendations."
            )
        else:
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
    st.markdown("Diagnose issues and get recommendations.")

    profile = _get_active_profile()
    if not profile:
        return

    # For imported clusters we don't need a CP node — commands run locally via kubeconfig
    cp_node = None
    if profile.cluster_source != "imported":
        cp_nodes = profile.get_control_plane_nodes()
        if not cp_nodes:
            st.error("No control-plane node defined in this profile.")
            return
        cp_node = cp_nodes[0]

    # kubectl availability warning for imported clusters
    if profile.cluster_source == "imported" and not get_kubectl_path():
        st.warning(
            "kubectl not found on this machine. Commands will fail until kubectl is installed.\n\n"
            "Install: `curl -LO https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl && chmod +x kubectl && mv kubectl ~/.local/bin/`"
        )

    available_commands = get_available_commands(profile)

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
                options=list(available_commands.keys()),
                default=["Node Status", "Pod Status (All Namespaces)", "Events (Recent)"],
            )
        with col2:
            run_all = st.checkbox("Run ALL diagnostics")

        if st.button("Run Diagnostics", type="primary"):
            if run_all:
                with st.spinner("Running all diagnostics..."):
                    results = run_all_diagnostics(cp_node, profile=profile)
            else:
                results = {}
                for check in selected_checks:
                    with st.spinner(f"Running: {check}..."):
                        results[check] = run_diagnostic(cp_node, check, profile=profile)

            st.session_state.debug_results = results

            for name, result in results.items():
                status_icon = "+" if result.success else "-"
                with st.expander(f"{'✅' if result.success else '❌'} {name}", expanded=not result.success):
                    st.code(result.stdout if result.success else result.stderr, language="text")

        if st.session_state.debug_results:
            if not is_llm_configured():
                st.info("Enable AI analysis by setting `LLM_API_URL` and `LLM_API_KEY` env vars.")
            elif st.button("Analyze with AI", type="secondary"):
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
                results = run_category_diagnostics(cp_node, category, profile=profile)

            for name, result in results.items():
                with st.expander(f"{'✅' if result.success else '❌'} {name}"):
                    st.code(result.stdout if result.success else result.stderr, language="text")

            if is_llm_configured():
                if st.button("Analyze Category with AI", key="cat_ai"):
                    with st.spinner("Analyzing..."):
                        analysis = analyze_diagnostics(results, profile=profile)
                        st.markdown(analysis)

    # ── Custom Command ────────────────────────────────────────────────────
    with tab_custom:
        st.markdown("### Run Custom Command")
        if profile.cluster_source == "imported":
            st.info("Commands run locally via kubectl using the imported kubeconfig.")
        else:
            st.warning("Commands execute on the control-plane node via SSH.")
        custom_cmd = st.text_area(
            "Command",
            placeholder="kubectl get pods -A -o wide",
            height=100,
        )
        if st.button("Execute", type="primary", key="exec_custom") and custom_cmd:
            with st.spinner("Executing..."):
                result = run_custom_command(cp_node, custom_cmd, profile=profile)
                if result.success:
                    st.code(result.stdout, language="text")
                else:
                    st.error("Command failed")
                    st.code(result.stderr, language="text")

    # ── AI Debug Assistant ────────────────────────────────────────────────
    with tab_ai:
        st.markdown("### AI Debug Assistant")
        if not is_llm_configured():
            st.info(
                "LLM is not configured. Set `LLM_API_URL` and `LLM_API_KEY` "
                "environment variables to enable AI-powered debugging."
            )
            st.markdown(
                "You can still use the **Quick Diagnostics**, **Category Scan**, and "
                "**Custom Command** tabs to collect diagnostic data without an LLM."
            )
        else:
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
                        pod_result = check_pod_issues(cp_node, profile=profile)
                        if pod_result.success and pod_result.stdout.strip():
                            collected_data += f"\n\nProblematic Pods:\n{pod_result.stdout}"
                            with st.expander("Problematic Pods"):
                                st.code(pod_result.stdout, language="text")

                if auto_collect:
                    with st.spinner("Collecting diagnostics..."):
                        diag_results = run_category_diagnostics(cp_node, "Cluster Overview", profile=profile)
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

    # For imported clusters we don't need a CP node
    cp_node = None
    if profile.cluster_source != "imported":
        cp_nodes = profile.get_control_plane_nodes()
        if not cp_nodes:
            st.error("No control-plane node defined in this profile.")
            return
        cp_node = cp_nodes[0]

    # Namespace selection — auto-fetch from cluster for imported clusters
    if profile.cluster_source == "imported" and profile.kubeconfig_content:
        cluster_ns = fetch_namespaces(profile.kubeconfig_content)
        if cluster_ns:
            # Ensure "monitoring" is an option even if it doesn't exist yet
            ns_options = cluster_ns if "monitoring" in cluster_ns else cluster_ns + ["monitoring"]
            default_idx = ns_options.index("monitoring") if "monitoring" in ns_options else 0
            namespace = st.selectbox("Monitoring Namespace", options=ns_options, index=default_idx, key="mon_ns")
        else:
            namespace = st.text_input("Monitoring Namespace", value="monitoring", key="mon_ns_txt")
    else:
        namespace = st.text_input("Monitoring Namespace", value="monitoring", key="mon_ns_txt")

    tab_install, tab_metrics, tab_dashboards, tab_alerts, tab_status, tab_scripts, tab_advice = st.tabs([
        "Install Stack",
        "Metrics Components",
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
                    result = install_helm(cp_node, profile=profile)
                    if result.success:
                        st.success("Helm ready!")
                    else:
                        st.error("Helm installation failed")
                        st.code(result.stderr, language="text")

            with st.status("Installing kube-prometheus-stack (this may take several minutes)...", expanded=True):
                result = install_prometheus_stack(cp_node, namespace, profile=profile)
                if result.success:
                    st.success("Prometheus + Grafana installed!")
                    st.code(result.stdout[-2000:], language="text")
                else:
                    st.error("Installation failed")
                    st.code(result.stderr, language="text")

            if install_alerts_too:
                with st.status("Installing alert rules...", expanded=True):
                    result = install_alert_rules(cp_node, namespace, profile=profile)
                    if result.success:
                        st.success("Alert rules installed!")
                    else:
                        st.error("Alert rules installation failed")
                        st.code(result.stderr, language="text")

    # ── Metrics Components ────────────────────────────────────────────────
    with tab_metrics:
        st.markdown("### Metrics Components")
        st.markdown(
            "Install **metrics-server** (enables `kubectl top`) and/or "
            "**kube-state-metrics** (exposes workload/object-level metrics to Prometheus)."
        )

        met_col1, met_col2 = st.columns(2)

        with met_col1:
            st.markdown("#### metrics-server")
            st.markdown(
                "Provides CPU/memory usage for pods and nodes. "
                "Required for `kubectl top` and HPA autoscaling."
            )
            ms_insecure = st.checkbox(
                "Add `--kubelet-insecure-tls` flag (self-signed certs)",
                value=True,
                key="ms_insecure",
            )
            if st.button("Install metrics-server", type="primary", key="install_ms"):
                ms_url = (
                    "https://github.com/kubernetes-sigs/metrics-server"
                    "/releases/latest/download/components.yaml"
                )
                with st.status("Installing metrics-server...", expanded=True):
                    # Apply the manifest
                    apply_result = run_kubectl(
                        profile,
                        f"apply -f {ms_url}",
                        timeout=60,
                    )
                    if apply_result.success:
                        st.write("Manifest applied successfully.")
                        st.code(apply_result.stdout, language="text")
                        # Patch for insecure TLS if requested
                        if ms_insecure:
                            patch_cmd = (
                                "patch deployment metrics-server -n kube-system "
                                "--type=json -p="
                                "'[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/0/args/-\","
                                "\"value\":\"--kubelet-insecure-tls\"}]'"
                            )
                            patch_result = run_kubectl(profile, patch_cmd, timeout=30)
                            if patch_result.success:
                                st.success("metrics-server installed with --kubelet-insecure-tls!")
                            else:
                                st.warning("Installed but TLS patch may have failed (already applied?).")
                                st.code(patch_result.stderr, language="text")
                        else:
                            st.success("metrics-server installed!")
                    else:
                        st.error("metrics-server installation failed")
                        st.code(apply_result.stderr, language="text")

            # Check status
            if st.button("Check metrics-server status", key="ms_status"):
                with st.spinner("Checking..."):
                    result = run_kubectl(
                        profile,
                        "get deployment metrics-server -n kube-system -o wide",
                        timeout=15,
                    )
                    if result.success:
                        st.code(result.stdout, language="text")
                    else:
                        st.warning("metrics-server not found or not ready.")
                        st.code(result.stderr, language="text")

        with met_col2:
            st.markdown("#### kube-state-metrics")
            st.markdown(
                "Exposes object-level metrics (Deployments, Pods, Nodes, etc.) "
                "to Prometheus for dashboards and alerting."
            )
            ksm_ns = namespace  # reuse the monitoring namespace
            if st.button("Install kube-state-metrics", type="primary", key="install_ksm"):
                with st.status("Installing kube-state-metrics...", expanded=True):
                    # Use helm if available, otherwise apply raw manifest
                    helm_cmd = (
                        f"helm install kube-state-metrics "
                        f"oci://registry-1.docker.io/bitnamicharts/kube-state-metrics "
                        f"-n {ksm_ns} --create-namespace"
                    )
                    result = run_kubectl(profile, helm_cmd, timeout=120)
                    if result.success:
                        st.success("kube-state-metrics installed via Helm!")
                        st.code(result.stdout, language="text")
                    else:
                        st.warning("Helm install failed, trying kubectl apply...")
                        st.code(result.stderr, language="text")
                        # Fallback: direct manifest from GitHub
                        ksm_url = (
                            "https://raw.githubusercontent.com/kubernetes/"
                            "kube-state-metrics/main/examples/standard/service.yaml"
                        )
                        apply_result = run_kubectl(
                            profile,
                            f"apply -f https://raw.githubusercontent.com/kubernetes/kube-state-metrics/main/examples/standard/ 2>/dev/null || echo 'Manual install required'",
                            timeout=60,
                        )
                        if apply_result.success:
                            st.success("kube-state-metrics applied!")
                            st.code(apply_result.stdout, language="text")
                        else:
                            st.error(
                                "Could not install kube-state-metrics automatically.\n\n"
                                "Manual install:\n"
                                "```\nhelm repo add prometheus-community "
                                "https://prometheus-community.github.io/helm-charts\n"
                                "helm install kube-state-metrics "
                                f"prometheus-community/kube-state-metrics -n {ksm_ns}\n```"
                            )

            if st.button("Check kube-state-metrics status", key="ksm_status"):
                with st.spinner("Checking..."):
                    result = run_kubectl(
                        profile,
                        f"get pods -n {ksm_ns} -l app.kubernetes.io/name=kube-state-metrics -o wide",
                        timeout=15,
                    )
                    if result.success and result.stdout.strip():
                        st.code(result.stdout, language="text")
                    else:
                        # Try broader search
                        result2 = run_kubectl(
                            profile,
                            "get pods -A -l app.kubernetes.io/name=kube-state-metrics -o wide",
                            timeout=15,
                        )
                        if result2.success and result2.stdout.strip():
                            st.code(result2.stdout, language="text")
                        else:
                            st.warning("kube-state-metrics not found on the cluster.")

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
                result = install_dashboards(cp_node, selected_dashboards, namespace, profile=profile)
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
                result = install_alert_rules(cp_node, namespace, profile=profile)
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
                result = get_monitoring_status(cp_node, namespace, profile=profile)
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
        if not is_llm_configured():
            st.info(
                "LLM is not configured. Set `LLM_API_URL` and `LLM_API_KEY` "
                "environment variables to enable AI-powered monitoring advice."
            )
        else:
            if st.button("Get Monitoring Recommendations", type="primary", key="mon_advice"):
                current_status = ""
                status_result = get_monitoring_status(cp_node, namespace, profile=profile)
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

    # For imported clusters we don't need a CP node
    cp_node = None
    if profile.cluster_source != "imported":
        cp_nodes = profile.get_control_plane_nodes()
        if not cp_nodes:
            st.error("No control-plane node defined in this profile.")
            return
        cp_node = cp_nodes[0]

    # Pre-fetch namespaces for imported clusters (used by Pod Logs tab)
    _cluster_namespaces: list[str] = []
    if profile.cluster_source == "imported" and profile.kubeconfig_content:
        _cluster_namespaces = fetch_namespaces(profile.kubeconfig_content)

    available_log_sources = get_available_log_sources(profile)

    tab_system, tab_pod, tab_correlation, tab_smart, tab_ai = st.tabs([
        "System Logs",
        "Pod Logs",
        "Error Correlation",
        "Smart Log Analysis",
        "AI Log Analysis",
    ])

    # ── System Logs ───────────────────────────────────────────────────────
    with tab_system:
        st.markdown("### System Component Logs")
        col1, col2, col3 = st.columns(3)
        with col1:
            default_sources = [s for s in ["Kubelet", "CRI-O", "Events"] if s in available_log_sources]
            if not default_sources:
                default_sources = available_log_sources[:3] if available_log_sources else []
            sources = st.multiselect(
                "Log Sources",
                options=available_log_sources,
                default=default_sources,
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
                    result = collect_logs(cp_node, source, log_lines, since, since_k8s, profile=profile)
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

        # --- Namespace selection ---
        col1, col2 = st.columns(2)
        with col1:
            if _cluster_namespaces:
                pod_ns = st.selectbox("Namespace", options=_cluster_namespaces,
                                      index=_cluster_namespaces.index("default") if "default" in _cluster_namespaces else 0,
                                      key="pod_ns")
            else:
                pod_ns = st.text_input("Namespace", value="default", key="pod_ns")
        with col2:
            pod_lines = st.number_input("Lines", min_value=50, max_value=5000, value=200, key="pod_lines")

        # --- Load pods from the cluster ---
        if st.button("Load Pods", key="load_pods_btn"):
            with st.spinner(f"Fetching pods in namespace '{pod_ns}'..."):
                pod_result = get_pod_list(cp_node, namespace=pod_ns, profile=profile)
                if pod_result.success and pod_result.stdout.strip():
                    _pods: list[dict] = []
                    for line in pod_result.stdout.strip().split("\n"):
                        parts = line.split()
                        if len(parts) >= 4:
                            _pods.append({
                                "namespace": parts[0],
                                "name": parts[1],
                                "status": parts[2],
                                "containers": parts[3],
                            })
                        elif len(parts) >= 2:
                            _pods.append({
                                "namespace": parts[0],
                                "name": parts[1],
                                "status": parts[2] if len(parts) > 2 else "Unknown",
                                "containers": parts[3] if len(parts) > 3 else "",
                            })
                    st.session_state["_pod_list"] = _pods
                    st.session_state["_pod_list_ns"] = pod_ns
                    st.success(f"Found {len(_pods)} pod(s) in namespace '{pod_ns}'.")
                elif pod_result.success:
                    st.session_state["_pod_list"] = []
                    st.session_state["_pod_list_ns"] = pod_ns
                    st.warning(f"No pods found in namespace '{pod_ns}'.")
                else:
                    st.error(f"Failed to fetch pods: {pod_result.stderr}")

        # --- Pod & container dropdowns ---
        _pods_loaded = st.session_state.get("_pod_list", [])
        _pods_loaded_ns = st.session_state.get("_pod_list_ns", "")

        col_p1, col_p2 = st.columns(2)
        with col_p1:
            if _pods_loaded and _pods_loaded_ns == pod_ns:
                pod_options = [f"{p['name']}  ({p['status']})" for p in _pods_loaded]
                selected_pod_idx = st.selectbox(
                    "Pod Name", options=range(len(pod_options)),
                    format_func=lambda i: pod_options[i],
                    key="pod_name_select",
                )
                pod_name = _pods_loaded[selected_pod_idx]["name"] if selected_pod_idx is not None else ""
            else:
                pod_name = st.text_input("Pod Name", placeholder="my-pod-xyz (click Load Pods to get dropdown)", key="pod_name_input")

        with col_p2:
            if _pods_loaded and _pods_loaded_ns == pod_ns and pod_name:
                # Find the selected pod's containers
                _selected_pod = next((p for p in _pods_loaded if p["name"] == pod_name), None)
                _containers: list[str] = []
                if _selected_pod and _selected_pod.get("containers"):
                    _containers = [c.strip() for c in _selected_pod["containers"].split(",") if c.strip()]
                if _containers:
                    container_options = ["(all / default)"] + _containers
                    container_sel = st.selectbox("Container", options=container_options, key="pod_container_select")
                    container = "" if container_sel == "(all / default)" else container_sel
                else:
                    container = st.text_input("Container (optional)", key="pod_container")
            else:
                container = st.text_input("Container (optional)", key="pod_container")

        pod_previous = st.checkbox("Previous container logs (crash recovery)")

        # --- Fetch logs ---
        if st.button("Fetch Pod Logs", type="primary", key="fetch_pod"):
            if not pod_name:
                st.warning("Please enter a pod name or click **Load Pods** to select one.")
            else:
                with st.spinner(f"Fetching logs for {pod_ns}/{pod_name}..."):
                    result = collect_pod_logs(
                        cp_node, pod_ns, pod_name, container, pod_lines,
                        "1h", pod_previous, profile=profile,
                    )
                    if result.success:
                        if not result.stdout.strip():
                            st.info(f"No log output returned for pod `{pod_ns}/{pod_name}`. "
                                    "The pod may have just started or has no recent logs.")
                        else:
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

                            if analysis.error_count > 0 and is_llm_configured():
                                if st.button("Analyze with AI", key="pod_ai"):
                                    with st.spinner("AI analyzing pod logs..."):
                                        ai_analysis = llm_analyze_logs(
                                            result.stdout, f"{pod_ns}/{pod_name}"
                                        )
                                        st.markdown(ai_analysis)
                    else:
                        st.error(f"Failed to fetch pod logs for `{pod_ns}/{pod_name}`")
                        st.code(result.stderr, language="text")

    # ── Error Correlation ─────────────────────────────────────────────────
    with tab_correlation:
        st.markdown("### Cross-Source Error Correlation")
        st.markdown("Collect logs from multiple sources and correlate errors across them.")

        default_corr = [s for s in ["Kubelet", "CRI-O", "API Server", "Events"] if s in available_log_sources]
        if not default_corr:
            default_corr = available_log_sources[:4] if available_log_sources else []
        corr_sources = st.multiselect(
            "Sources to correlate",
            options=available_log_sources,
            default=default_corr,
            key="corr_sources",
        )

        if st.button("Collect & Correlate", type="primary", key="correlate"):
            with st.spinner("Collecting logs from multiple sources..."):
                results = collect_multi_source_logs(cp_node, corr_sources, lines=150, profile=profile)

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
            if is_llm_configured():
                if st.button("Deep AI Correlation Analysis", key="deep_corr"):
                    multi_logs = {
                        src: res.stdout for src, res in results.items() if res.success
                    }
                    with st.spinner("AI is performing deep correlation analysis..."):
                        analysis = llm_correlate_analysis(multi_logs)
                        st.markdown(analysis)

    # ── Smart Log Analysis (LogAI-inspired) ─────────────────────────────
    with tab_smart:
        st.markdown("### Smart Log Analysis (LogAI-inspired)")
        st.markdown(
            "ML-powered log analysis using techniques from "
            "[Salesforce LogAI](https://github.com/salesforce/logai): "
            "**log clustering** (TF-IDF + DBSCAN), **anomaly detection**, "
            "**pattern mining** (Drain-style), and **auto-summarization**."
        )

        smart_mode = st.radio(
            "Analysis mode",
            ["Collect from cluster", "Paste logs"],
            horizontal=True,
            key="smart_mode",
        )

        smart_log_text = ""

        if smart_mode == "Collect from cluster":
            scol1, scol2, scol3 = st.columns(3)
            with scol1:
                smart_source = st.selectbox(
                    "Log Source", available_log_sources, key="smart_source",
                )
            with scol2:
                smart_lines = st.number_input(
                    "Lines to fetch", min_value=100, max_value=5000, value=500, key="smart_lines",
                )
            with scol3:
                smart_since_opts = {
                    "Last 15 min": ("15 minutes ago", "15m"),
                    "Last 1 hour": ("1 hour ago", "1h"),
                    "Last 6 hours": ("6 hours ago", "6h"),
                    "Last 24 hours": ("24 hours ago", "24h"),
                }
                smart_since_label = st.selectbox(
                    "Time Range", list(smart_since_opts.keys()), index=1, key="smart_since",
                )
                smart_since, smart_since_k8s = smart_since_opts[smart_since_label]

            if st.button("Collect & Analyze", type="primary", key="smart_collect"):
                with st.spinner(f"Collecting {smart_source} logs..."):
                    result = collect_logs(
                        cp_node, smart_source, smart_lines, smart_since, smart_since_k8s, profile=profile,
                    )
                if result.success and result.stdout.strip():
                    smart_log_text = result.stdout
                    st.session_state["_smart_log_text"] = smart_log_text
                    st.session_state["_smart_source"] = smart_source
                elif result.success:
                    st.info("No logs returned for the selected source and time range.")
                else:
                    st.error(f"Failed to collect logs: {result.stderr}")

            # Persist across reruns
            if "_smart_log_text" in st.session_state and not smart_log_text:
                smart_log_text = st.session_state["_smart_log_text"]

        else:
            smart_log_text = st.text_area(
                "Paste log output",
                height=200,
                placeholder="Paste your Kubernetes logs here for smart analysis...",
                key="smart_paste",
            )
            if smart_log_text:
                st.session_state["_smart_log_text"] = smart_log_text
                st.session_state["_smart_source"] = "pasted"

        # Run analysis if we have log text
        if smart_log_text:
            src_label = st.session_state.get("_smart_source", "")
            with st.spinner("Running LogAI-inspired analysis pipeline..."):
                sa_result = smart_analyze(smart_log_text, source=src_label)

            # ── Summary / Health Score ────────────────────────────────
            st.markdown("---")
            st.markdown("#### Log Summary & Health Score")
            summary = sa_result.summary
            health = summary.get("health_score", 100)
            health_color = "green" if health >= 80 else ("orange" if health >= 50 else "red")
            scol1, scol2, scol3, scol4, scol5 = st.columns(5)
            scol1.metric("Total Lines", summary.get("total_lines", 0))
            scol2.metric("Errors", summary.get("error_count", 0))
            scol3.metric("Warnings", summary.get("warning_count", 0))
            scol4.metric("Unique Templates", summary.get("unique_templates", 0))
            scol5.metric("Health Score", f"{health}/100")

            if health < 50:
                st.error(f"Health score is **{health}/100** — significant issues detected in logs.")
            elif health < 80:
                st.warning(f"Health score is **{health}/100** — some issues detected.")
            else:
                st.success(f"Health score is **{health}/100** — logs look healthy.")

            st.markdown(
                f"**Time span:** {summary.get('first_timestamp', 'N/A')} → {summary.get('last_timestamp', 'N/A')} | "
                f"**Template diversity:** {summary.get('template_diversity', 0)}%"
            )

            # Top errors
            top_errors = summary.get("top_errors", [])
            if top_errors:
                with st.expander(f"Top {len(top_errors)} Error Patterns", expanded=True):
                    for pattern, count in top_errors:
                        st.markdown(f"- **x{count}** — `{pattern[:200]}`")

            # ── Log Clustering ────────────────────────────────────────
            st.markdown("---")
            st.markdown("#### Log Clustering (TF-IDF + DBSCAN)")
            st.markdown(
                "Groups similar log messages together to reduce noise and highlight distinct message types. "
                "Uses TF-IDF vectorization and DBSCAN density-based clustering."
            )
            if sa_result.clusters:
                import pandas as pd
                cluster_data = []
                for c in sa_result.clusters:
                    label = f"Cluster {c.cluster_id}" if c.cluster_id >= 0 else "Noise (unique)"
                    cluster_data.append({
                        "Cluster": label,
                        "Count": c.count,
                        "Level": c.level,
                        "Template": c.template[:120],
                        "First Seen": c.first_seen or "N/A",
                        "Last Seen": c.last_seen or "N/A",
                    })
                df_clusters = pd.DataFrame(cluster_data)
                st.dataframe(df_clusters, use_container_width=True, hide_index=True)

                # Cluster distribution chart
                try:
                    import plotly.express as px
                    fig = px.pie(
                        df_clusters, names="Cluster", values="Count",
                        title="Log Message Distribution by Cluster",
                        color_discrete_sequence=px.colors.qualitative.Set3,
                    )
                    fig.update_layout(height=400)
                    st.plotly_chart(fig, use_container_width=True)
                except ImportError:
                    pass

                # Show sample messages per cluster
                error_clusters = [c for c in sa_result.clusters if c.level == "ERROR"]
                if error_clusters:
                    with st.expander(f"Error Clusters ({len(error_clusters)})", expanded=True):
                        for c in error_clusters:
                            label = f"Cluster {c.cluster_id}" if c.cluster_id >= 0 else "Noise"
                            st.markdown(f"**{label}** — {c.count} messages")
                            for sample in c.sample_messages[:2]:
                                st.code(sample, language="text")
            else:
                st.info("Not enough log lines for clustering (need 3+ lines).")

            # ── Anomaly Detection ─────────────────────────────────────
            st.markdown("---")
            st.markdown("#### Anomaly Detection")
            st.markdown(
                "Detects unusual log lines using TF-IDF distance from centroid (outlier scoring) "
                "and frequency-based rare template detection."
            )
            if sa_result.anomalies:
                st.markdown(f"**{len(sa_result.anomalies)} anomalous log line(s) detected**")
                import pandas as pd
                anomaly_data = []
                for a in sa_result.anomalies[:30]:
                    anomaly_data.append({
                        "Score": round(a.score, 2),
                        "Reason": a.reason,
                        "Timestamp": a.timestamp or "N/A",
                        "Message": a.message[:150],
                    })
                df_anomalies = pd.DataFrame(anomaly_data)
                st.dataframe(df_anomalies, use_container_width=True, hide_index=True)

                # Show full messages for top anomalies
                with st.expander("Top Anomaly Details", expanded=False):
                    for i, a in enumerate(sa_result.anomalies[:10]):
                        st.markdown(f"**#{i+1}** (score: {a.score:.2f}) — {a.reason}")
                        st.code(a.message, language="text")
            else:
                st.success("No anomalous log lines detected — all messages follow expected patterns.")

            # ── Pattern Mining ────────────────────────────────────────
            st.markdown("---")
            st.markdown("#### Pattern Mining (Drain-style)")
            st.markdown(
                "Extracts frequent log templates by replacing variable tokens (IPs, IDs, numbers, paths) "
                "with placeholders — similar to LogAI's Drain parser."
            )
            if sa_result.patterns:
                import pandas as pd
                pattern_data = []
                for p in sa_result.patterns[:20]:
                    pattern_data.append({
                        "Template": p["template"][:120],
                        "Count": p["count"],
                        "% of Logs": p["percentage"],
                        "Level": p["level"],
                    })
                df_patterns = pd.DataFrame(pattern_data)
                st.dataframe(df_patterns, use_container_width=True, hide_index=True)

                # Bar chart of top patterns
                try:
                    import plotly.express as px
                    top_10 = sa_result.patterns[:10]
                    fig = px.bar(
                        x=[p["template"][:60] for p in top_10],
                        y=[p["count"] for p in top_10],
                        labels={"x": "Template", "y": "Count"},
                        title="Top 10 Log Templates",
                        color=[p["level"] for p in top_10],
                        color_discrete_map={"ERROR": "#FF4B4B", "WARNING": "#FFA500", "INFO": "#326CE5"},
                    )
                    fig.update_layout(height=400, xaxis_tickangle=-45)
                    st.plotly_chart(fig, use_container_width=True)
                except ImportError:
                    pass
            else:
                st.info("No patterns extracted.")

            # ── Timeline ──────────────────────────────────────────────
            if sa_result.timeline_buckets and len(sa_result.timeline_buckets) > 1:
                st.markdown("---")
                st.markdown("#### Log Volume Timeline")
                try:
                    import plotly.graph_objects as go
                    import pandas as pd
                    ts_labels = [b["timestamp"] for b in sa_result.timeline_buckets if b["timestamp"] != "unknown"]
                    ts_totals = [b["total"] for b in sa_result.timeline_buckets if b["timestamp"] != "unknown"]
                    ts_errors = [b["errors"] for b in sa_result.timeline_buckets if b["timestamp"] != "unknown"]
                    ts_warnings = [b["warnings"] for b in sa_result.timeline_buckets if b["timestamp"] != "unknown"]

                    if ts_labels:
                        fig = go.Figure()
                        fig.add_trace(go.Scatter(x=ts_labels, y=ts_totals, name="Total", mode="lines+markers", line=dict(color="#326CE5")))
                        fig.add_trace(go.Bar(x=ts_labels, y=ts_errors, name="Errors", marker_color="#FF4B4B"))
                        fig.add_trace(go.Bar(x=ts_labels, y=ts_warnings, name="Warnings", marker_color="#FFA500"))
                        fig.update_layout(
                            title="Log Volume Over Time",
                            yaxis_title="Count",
                            xaxis_title="Time",
                            barmode="stack",
                            height=400,
                        )
                        st.plotly_chart(fig, use_container_width=True)
                except ImportError:
                    pass

    # ── AI Log Analysis ───────────────────────────────────────────────────
    with tab_ai:
        st.markdown("### AI-Powered Log Analysis")
        if not is_llm_configured():
            st.info(
                "LLM is not configured. Set `LLM_API_URL` and `LLM_API_KEY` "
                "environment variables to enable AI-powered log analysis."
            )
            st.markdown(
                "You can still use the **System Logs**, **Pod Logs**, and "
                "**Error Correlation** tabs — they work without an LLM and provide "
                "automated pattern matching and error grouping."
            )
        else:
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

# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Resource Viewer
# ══════════════════════════════════════════════════════════════════════════

# Resource definitions: (display_name, kubectl_command, supports_namespace)
_RESOURCE_TYPES = {
    "Pods": ("get pods", True),
    "Deployments": ("get deployments", True),
    "Services": ("get services", True),
    "ConfigMaps": ("get configmaps", True),
    "Secrets": ("get secrets", True),
    "StatefulSets": ("get statefulsets", True),
    "DaemonSets": ("get daemonsets", True),
    "ReplicaSets": ("get replicasets", True),
    "Jobs": ("get jobs", True),
    "CronJobs": ("get cronjobs", True),
    "Ingresses": ("get ingress", True),
    "NetworkPolicies": ("get networkpolicies", True),
    "PersistentVolumeClaims": ("get pvc", True),
    "PersistentVolumes": ("get pv", False),
    "StorageClasses": ("get storageclasses", False),
    "Namespaces": ("get namespaces", False),
    "Nodes": ("get nodes", False),
    "ServiceAccounts": ("get serviceaccounts", True),
    "DestinationRules": ("get destinationrules", True),
    "VirtualServices": ("get virtualservices", True),
    "HorizontalPodAutoscalers": ("get hpa", True),
    "PodDisruptionBudgets": ("get pdb", True),
    "Endpoints": ("get endpoints", True),
}


def page_resource_viewer():
    st.markdown("## Resource Viewer")
    st.markdown("Browse live Kubernetes resources from your cluster.")

    profile = _get_active_profile()
    if not profile:
        return

    if not profile.kubeconfig_content and not profile.get_control_plane_nodes():
        st.error(
            "This profile has no kubeconfig and no control-plane node. "
            "Import a kubeconfig or add nodes in the Profile Manager."
        )
        return

    # Pre-fetch namespaces for imported clusters
    _rv_namespaces: list[str] = []
    if profile.cluster_source == "imported" and profile.kubeconfig_content:
        _rv_namespaces = fetch_namespaces(profile.kubeconfig_content)

    (tab_resources, tab_scaling, tab_shell, tab_res_limits, tab_crictl,
     tab_node_health, tab_rbac, tab_helm, tab_events,
     tab_restart_tracker, tab_netpol, tab_pvc) = st.tabs([
        "Cluster Resources",
        "Scaling",
        "Pod Shell",
        "Resource Requests/Limits",
        "Node Containers",
        "Node Health",
        "RBAC Viewer",
        "Helm Releases",
        "Events Timeline",
        "Pod Restart Tracker",
        "Network Policies",
        "PVC / Storage",
    ])

    # ── Cluster Resources ────────────────────────────────────────────────
    with tab_resources:
        st.markdown("### Browse Cluster Resources")

        col1, col2, col3 = st.columns([2, 2, 1])
        with col1:
            resource_type = st.selectbox(
                "Resource Type",
                options=list(_RESOURCE_TYPES.keys()),
                index=0,
            )
        with col2:
            cmd_base, ns_supported = _RESOURCE_TYPES[resource_type]
            if ns_supported:
                ns_choice = st.radio(
                    "Namespace",
                    ["All Namespaces", "Specific"],
                    horizontal=True,
                    key="res_ns_choice",
                )
                if ns_choice == "Specific":
                    if _rv_namespaces:
                        namespace = st.selectbox(
                            "Namespace", options=_rv_namespaces,
                            index=_rv_namespaces.index("default") if "default" in _rv_namespaces else 0,
                            key="res_ns",
                        )
                    else:
                        namespace = st.text_input("Namespace", value="default", key="res_ns")
                else:
                    namespace = ""
            else:
                namespace = ""
                st.info(f"{resource_type} is a cluster-scoped resource.")
        with col3:
            output_format = st.selectbox(
                "Output",
                ["wide", "yaml", "json", "name"],
                index=0,
                key="res_output",
            )

        if st.button("Fetch Resources", type="primary", key="fetch_res"):
            kubectl_cmd = cmd_base
            if ns_supported and not namespace:
                kubectl_cmd += " -A"
            elif ns_supported and namespace:
                kubectl_cmd += f" -n {namespace}"
            kubectl_cmd += f" -o {output_format}"

            with st.spinner(f"Fetching {resource_type}..."):
                result = run_kubectl(profile, kubectl_cmd, timeout=30)
                if result.success:
                    st.code(result.stdout or "(no resources found)", language="text")
                else:
                    if "the server doesn't have a resource type" in result.stderr:
                        st.warning(
                            f"{resource_type} is not available on this cluster "
                            "(CRD may not be installed)."
                        )
                    else:
                        st.error("Failed to fetch resources")
                    st.code(result.stderr, language="text")

        # Describe a specific resource
        st.markdown("---")
        st.markdown("#### Describe a Resource")
        desc_col1, desc_col2, desc_col3 = st.columns([2, 2, 1])
        with desc_col2:
            if _rv_namespaces:
                desc_ns = st.selectbox(
                    "Namespace",
                    options=_rv_namespaces,
                    index=_rv_namespaces.index("default") if "default" in _rv_namespaces else 0,
                    key="desc_ns",
                )
            else:
                desc_ns = st.text_input(
                    "Namespace",
                    value="default",
                    key="desc_ns",
                )
        with desc_col3:
            desc_refresh = st.button("Load names", key="desc_load_names")

        # Fetch resource names for the dropdown
        _desc_resource_names: list[str] = []
        if desc_refresh or st.session_state.get("_desc_cached_names"):
            if desc_refresh:
                # Determine the kubectl get command for names
                _desc_cmd_base, _desc_ns_supported = _RESOURCE_TYPES[resource_type]
                _names_cmd = f"{_desc_cmd_base} -o name"
                if _desc_ns_supported and desc_ns:
                    _names_cmd += f" -n {desc_ns}"
                elif _desc_ns_supported:
                    _names_cmd += " -A"
                _names_result = run_kubectl(profile, _names_cmd, timeout=15)
                if _names_result.success and _names_result.stdout.strip():
                    raw_names = _names_result.stdout.strip().split("\n")
                    # Strip resource type prefix (e.g. "pod/my-pod" -> "my-pod")
                    _desc_resource_names = [
                        n.split("/", 1)[-1] if "/" in n else n
                        for n in raw_names if n.strip()
                    ]
                    st.session_state["_desc_cached_names"] = _desc_resource_names
                    st.session_state["_desc_cached_type"] = resource_type
                else:
                    _desc_resource_names = []
                    st.session_state["_desc_cached_names"] = []
            else:
                # Use cached names if resource type matches
                if st.session_state.get("_desc_cached_type") == resource_type:
                    _desc_resource_names = st.session_state.get("_desc_cached_names", [])

        with desc_col1:
            if _desc_resource_names:
                desc_name = st.selectbox(
                    "Resource name",
                    options=_desc_resource_names,
                    key="desc_name_select",
                )
            else:
                desc_name = st.text_input(
                    "Resource name",
                    placeholder="Click 'Load names' or type a name",
                    key="desc_name",
                )

        if st.button("Describe", key="describe_res") and desc_name:
            # Determine the singular resource type for describe
            res_singular = resource_type.rstrip("s")
            if resource_type == "Ingresses":
                res_singular = "ingress"
            elif resource_type == "Namespaces":
                res_singular = "namespace"
            elif resource_type == "StorageClasses":
                res_singular = "storageclass"
            elif resource_type == "Endpoints":
                res_singular = "endpoints"

            desc_cmd = f"describe {res_singular.lower()} {desc_name}"
            if ns_supported and desc_ns:
                desc_cmd += f" -n {desc_ns}"

            with st.spinner(f"Describing {desc_name}..."):
                result = run_kubectl(profile, desc_cmd, timeout=30)
                if result.success:
                    st.code(result.stdout, language="yaml")
                else:
                    st.error("Describe failed")
                    st.code(result.stderr, language="text")

    # ── Scaling ──────────────────────────────────────────────────────────
    with tab_scaling:
        st.markdown("### Deployment Scaling")
        st.markdown("Scale deployment replicas up or down.")

        sc_col1, sc_col2 = st.columns(2)
        with sc_col1:
            if _rv_namespaces:
                sc_ns = st.selectbox(
                    "Namespace",
                    options=_rv_namespaces,
                    index=_rv_namespaces.index("default") if "default" in _rv_namespaces else 0,
                    key="sc_ns",
                )
            else:
                sc_ns = st.text_input("Namespace", value="default", key="sc_ns")
        with sc_col2:
            sc_load = st.button("Load Deployments", key="sc_load")

        # Fetch deployments for the dropdown
        _sc_deployments: list[str] = []
        _sc_dep_info: dict[str, str] = {}
        if sc_load or st.session_state.get("_sc_cached_deps"):
            if sc_load:
                dep_result = run_kubectl(
                    profile,
                    f"get deployments -n {sc_ns} -o custom-columns=NAME:.metadata.name,REPLICAS:.spec.replicas,AVAILABLE:.status.availableReplicas --no-headers",
                    timeout=15,
                )
                if dep_result.success and dep_result.stdout.strip():
                    for line in dep_result.stdout.strip().split("\n"):
                        parts = line.split()
                        if parts:
                            dep_name = parts[0]
                            _sc_deployments.append(dep_name)
                            replicas = parts[1] if len(parts) > 1 else "?"
                            available = parts[2] if len(parts) > 2 else "?"
                            _sc_dep_info[dep_name] = f"{replicas} replicas ({available} available)"
                    st.session_state["_sc_cached_deps"] = _sc_deployments
                    st.session_state["_sc_cached_dep_info"] = _sc_dep_info
                    st.session_state["_sc_cached_ns"] = sc_ns
                else:
                    st.session_state["_sc_cached_deps"] = []
                    st.session_state["_sc_cached_dep_info"] = {}
                    if dep_result.success:
                        st.info(f"No deployments found in namespace '{sc_ns}'.")
                    else:
                        st.error("Failed to list deployments")
                        st.code(dep_result.stderr, language="text")
            else:
                if st.session_state.get("_sc_cached_ns") == sc_ns:
                    _sc_deployments = st.session_state.get("_sc_cached_deps", [])
                    _sc_dep_info = st.session_state.get("_sc_cached_dep_info", {})

        if _sc_deployments:
            sc_dep_col1, sc_dep_col2, sc_dep_col3 = st.columns([3, 1, 1])
            with sc_dep_col1:
                sc_selected = st.selectbox(
                    "Deployment",
                    options=_sc_deployments,
                    format_func=lambda d: f"{d}  ({_sc_dep_info.get(d, '')})",
                    key="sc_selected",
                )
            with sc_dep_col2:
                sc_replicas = st.number_input(
                    "Target replicas",
                    min_value=0,
                    max_value=100,
                    value=1,
                    key="sc_replicas",
                )
            with sc_dep_col3:
                st.markdown("<br>", unsafe_allow_html=True)
                if st.button("Scale", type="primary", key="sc_apply"):
                    scale_cmd = f"scale deployment {sc_selected} --replicas={sc_replicas} -n {sc_ns}"
                    with st.spinner(f"Scaling {sc_selected} to {sc_replicas} replicas..."):
                        result = run_kubectl(profile, scale_cmd, timeout=30)
                        if result.success:
                            st.success(f"Scaled **{sc_selected}** to **{sc_replicas}** replicas!")
                            st.code(result.stdout, language="text")
                            # Refresh to show updated state
                            verify = run_kubectl(
                                profile,
                                f"get deployment {sc_selected} -n {sc_ns} -o wide",
                                timeout=15,
                            )
                            if verify.success:
                                st.code(verify.stdout, language="text")
                        else:
                            st.error("Scaling failed")
                            st.code(result.stderr, language="text")

            # Quick scale buttons
            st.markdown("---")
            st.markdown("#### Quick Actions")
            qa_col1, qa_col2, qa_col3, qa_col4 = st.columns(4)
            with qa_col1:
                if st.button("Scale to 0 (stop)", key="sc_0"):
                    with st.spinner("Scaling to 0..."):
                        result = run_kubectl(profile, f"scale deployment {sc_selected} --replicas=0 -n {sc_ns}", timeout=30)
                        st.success("Scaled to 0") if result.success else st.error(result.stderr)
            with qa_col2:
                if st.button("Scale to 1", key="sc_1"):
                    with st.spinner("Scaling to 1..."):
                        result = run_kubectl(profile, f"scale deployment {sc_selected} --replicas=1 -n {sc_ns}", timeout=30)
                        st.success("Scaled to 1") if result.success else st.error(result.stderr)
            with qa_col3:
                if st.button("Scale to 3", key="sc_3"):
                    with st.spinner("Scaling to 3..."):
                        result = run_kubectl(profile, f"scale deployment {sc_selected} --replicas=3 -n {sc_ns}", timeout=30)
                        st.success("Scaled to 3") if result.success else st.error(result.stderr)
            with qa_col4:
                if st.button("Scale to 5", key="sc_5"):
                    with st.spinner("Scaling to 5..."):
                        result = run_kubectl(profile, f"scale deployment {sc_selected} --replicas=5 -n {sc_ns}", timeout=30)
                        st.success("Scaled to 5") if result.success else st.error(result.stderr)
        elif not sc_load:
            st.info("Click **Load Deployments** to see deployments in the selected namespace.")

    # ── Pod Shell ────────────────────────────────────────────────────────
    with tab_shell:
        st.markdown("### Pod Shell (Exec)")
        st.markdown("Execute commands inside a running pod/container.")

        sh_col1, sh_col2 = st.columns(2)
        with sh_col1:
            if _rv_namespaces:
                sh_ns = st.selectbox(
                    "Namespace",
                    options=_rv_namespaces,
                    index=_rv_namespaces.index("default") if "default" in _rv_namespaces else 0,
                    key="sh_ns",
                )
            else:
                sh_ns = st.text_input("Namespace", value="default", key="sh_ns")
        with sh_col2:
            sh_load = st.button("Load Pods", key="sh_load")

        # Fetch running pods
        _sh_pods: list[str] = []
        _sh_containers: dict[str, list[str]] = {}
        if sh_load or st.session_state.get("_sh_cached_pods"):
            if sh_load:
                pod_result = run_kubectl(
                    profile,
                    f"get pods -n {sh_ns} --field-selector=status.phase=Running -o jsonpath="
                    "'{range .items[*]}{.metadata.name}{\"\\n\"}{end}'",
                    timeout=15,
                )
                if pod_result.success and pod_result.stdout.strip():
                    _sh_pods = [p.strip() for p in pod_result.stdout.strip().split("\n") if p.strip()]
                    st.session_state["_sh_cached_pods"] = _sh_pods
                    st.session_state["_sh_cached_ns"] = sh_ns
                    # Fetch container names for each pod
                    _sh_containers = {}
                    for pod_name in _sh_pods[:20]:  # limit to first 20 for perf
                        ctr_result = run_kubectl(
                            profile,
                            f"get pod {pod_name} -n {sh_ns} -o jsonpath="
                            "'{range .spec.containers[*]}{.name}{\"\\n\"}{end}'",
                            timeout=10,
                        )
                        if ctr_result.success and ctr_result.stdout.strip():
                            _sh_containers[pod_name] = [
                                c.strip() for c in ctr_result.stdout.strip().split("\n") if c.strip()
                            ]
                        else:
                            _sh_containers[pod_name] = []
                    st.session_state["_sh_cached_containers"] = _sh_containers
                else:
                    st.session_state["_sh_cached_pods"] = []
                    st.session_state["_sh_cached_containers"] = {}
                    if pod_result.success:
                        st.info(f"No running pods found in namespace '{sh_ns}'.")
                    else:
                        st.error("Failed to list pods")
                        st.code(pod_result.stderr, language="text")
            else:
                if st.session_state.get("_sh_cached_ns") == sh_ns:
                    _sh_pods = st.session_state.get("_sh_cached_pods", [])
                    _sh_containers = st.session_state.get("_sh_cached_containers", {})

        if _sh_pods:
            sh_pod_col1, sh_pod_col2 = st.columns(2)
            with sh_pod_col1:
                sh_selected_pod = st.selectbox("Pod", options=_sh_pods, key="sh_pod")
            with sh_pod_col2:
                containers = _sh_containers.get(sh_selected_pod, [])
                if containers:
                    sh_selected_ctr = st.selectbox("Container", options=containers, key="sh_ctr")
                else:
                    sh_selected_ctr = st.text_input("Container (optional)", key="sh_ctr")

            st.info(
                "**Note:** This runs non-interactive commands via `kubectl exec`. "
                "For a fully interactive shell, use your terminal:\n\n"
                f"`kubectl exec -it {sh_selected_pod} -n {sh_ns}"
                f"{' -c ' + sh_selected_ctr if sh_selected_ctr else ''} -- /bin/sh`"
            )

            sh_cmd = st.text_input(
                "Command to execute",
                value="sh -c 'hostname && cat /etc/os-release && df -h'",
                key="sh_cmd",
                help="Enter the command to run inside the container",
            )

            sh_preset_col1, sh_preset_col2, sh_preset_col3, sh_preset_col4 = st.columns(4)
            with sh_preset_col1:
                if st.button("env", key="sh_p_env"):
                    sh_cmd = "env"
            with sh_preset_col2:
                if st.button("ps aux", key="sh_p_ps"):
                    sh_cmd = "ps aux"
            with sh_preset_col3:
                if st.button("df -h", key="sh_p_df"):
                    sh_cmd = "df -h"
            with sh_preset_col4:
                if st.button("cat /etc/resolv.conf", key="sh_p_dns"):
                    sh_cmd = "cat /etc/resolv.conf"

            if st.button("Execute", type="primary", key="sh_exec") and sh_cmd:
                ctr_flag = f" -c {sh_selected_ctr}" if sh_selected_ctr else ""
                exec_cmd = f"exec {sh_selected_pod} -n {sh_ns}{ctr_flag} -- {sh_cmd}"
                with st.spinner(f"Executing in {sh_selected_pod}..."):
                    result = run_kubectl(profile, exec_cmd, timeout=30)
                    if result.success:
                        st.code(result.stdout or "(no output)", language="text")
                    else:
                        st.error("Exec failed")
                        st.code(result.stderr, language="text")

            # Pod logs quick access
            st.markdown("---")
            st.markdown("#### Quick Pod Logs")
            log_lines = st.number_input("Tail lines", min_value=10, max_value=500, value=50, key="sh_log_lines")
            if st.button("View Logs", key="sh_logs"):
                ctr_flag = f" -c {sh_selected_ctr}" if sh_selected_ctr else ""
                log_cmd = f"logs {sh_selected_pod} -n {sh_ns}{ctr_flag} --tail={log_lines}"
                with st.spinner("Fetching logs..."):
                    result = run_kubectl(profile, log_cmd, timeout=30)
                    if result.success:
                        st.code(result.stdout or "(no logs)", language="text")
                    else:
                        st.error("Failed to fetch logs")
                        st.code(result.stderr, language="text")
        elif not sh_load:
            st.info("Click **Load Pods** to see running pods in the selected namespace.")

    # ── Resource Requests / Limits ───────────────────────────────────────
    with tab_res_limits:
        st.markdown("### Container Resource Requests & Limits")
        st.markdown(
            "View CPU, memory, and ephemeral-storage requests and limits for all "
            "containers in a namespace (from Deployments, StatefulSets, DaemonSets, and Jobs)."
        )

        rl_col1, rl_col2 = st.columns(2)
        with rl_col1:
            if _rv_namespaces:
                rl_ns = st.selectbox(
                    "Namespace",
                    options=_rv_namespaces,
                    index=_rv_namespaces.index("default") if "default" in _rv_namespaces else 0,
                    key="rl_ns",
                )
            else:
                rl_ns = st.text_input("Namespace", value="default", key="rl_ns")
        with rl_col2:
            rl_workload = st.selectbox(
                "Workload Type",
                options=["Deployments", "StatefulSets", "DaemonSets", "Jobs", "All"],
                index=0,
                key="rl_workload",
            )

        if st.button("Fetch Resource Requests/Limits", type="primary", key="rl_fetch"):
            import json as _json

            workload_map = {
                "Deployments": "deploy",
                "StatefulSets": "statefulsets",
                "DaemonSets": "daemonsets",
                "Jobs": "jobs",
            }
            if rl_workload == "All":
                types_to_fetch = list(workload_map.items())
            else:
                types_to_fetch = [(rl_workload, workload_map[rl_workload])]

            all_rows: list[dict] = []
            for wl_label, wl_cmd in types_to_fetch:
                with st.spinner(f"Fetching {wl_label}..."):
                    result = run_kubectl(
                        profile,
                        f"get {wl_cmd} -n {rl_ns} -o json",
                        timeout=30,
                    )
                    if result.success and result.stdout.strip():
                        try:
                            data = _json.loads(result.stdout)
                            for item in data.get("items", []):
                                workload_name = item.get("metadata", {}).get("name", "?")
                                spec = item.get("spec", {})
                                # For Jobs the pod template is at spec.template,
                                # for Deployments/StatefulSets/DaemonSets it's spec.template
                                template = spec.get("template", {})
                                pod_spec = template.get("spec", {})
                                containers = pod_spec.get("containers", [])
                                init_containers = pod_spec.get("initContainers", [])
                                for ctr in containers:
                                    res = ctr.get("resources", {})
                                    req = res.get("requests", {})
                                    lim = res.get("limits", {})
                                    all_rows.append({
                                        "Type": wl_label,
                                        "Workload": workload_name,
                                        "Container": ctr.get("name", "?"),
                                        "Init": "",
                                        "CPU Req": req.get("cpu", "-"),
                                        "CPU Lim": lim.get("cpu", "-"),
                                        "Mem Req": req.get("memory", "-"),
                                        "Mem Lim": lim.get("memory", "-"),
                                        "Eph Req": req.get("ephemeral-storage", "-"),
                                        "Eph Lim": lim.get("ephemeral-storage", "-"),
                                    })
                                for ctr in init_containers:
                                    res = ctr.get("resources", {})
                                    req = res.get("requests", {})
                                    lim = res.get("limits", {})
                                    all_rows.append({
                                        "Type": wl_label,
                                        "Workload": workload_name,
                                        "Container": ctr.get("name", "?"),
                                        "Init": "init",
                                        "CPU Req": req.get("cpu", "-"),
                                        "CPU Lim": lim.get("cpu", "-"),
                                        "Mem Req": req.get("memory", "-"),
                                        "Mem Lim": lim.get("memory", "-"),
                                        "Eph Req": req.get("ephemeral-storage", "-"),
                                        "Eph Lim": lim.get("ephemeral-storage", "-"),
                                    })
                        except _json.JSONDecodeError:
                            st.warning(f"Could not parse JSON for {wl_label}")
                    elif not result.success:
                        st.warning(f"Failed to fetch {wl_label}: {result.stderr}")

            if all_rows:
                st.markdown(f"**{len(all_rows)} container(s)** found in namespace `{rl_ns}`:")
                st.dataframe(
                    all_rows,
                    use_container_width=True,
                    column_config={
                        "Type": st.column_config.TextColumn(width="small"),
                        "Workload": st.column_config.TextColumn(width="medium"),
                        "Container": st.column_config.TextColumn(width="medium"),
                        "Init": st.column_config.TextColumn(width="small"),
                        "CPU Req": st.column_config.TextColumn(width="small"),
                        "CPU Lim": st.column_config.TextColumn(width="small"),
                        "Mem Req": st.column_config.TextColumn(width="small"),
                        "Mem Lim": st.column_config.TextColumn(width="small"),
                        "Eph Req": st.column_config.TextColumn(width="small"),
                        "Eph Lim": st.column_config.TextColumn(width="small"),
                    },
                )

                # Summary stats
                st.markdown("---")
                st.markdown("#### Summary")
                no_cpu_req = sum(1 for r in all_rows if r["CPU Req"] == "-" and r["Init"] == "")
                no_mem_req = sum(1 for r in all_rows if r["Mem Req"] == "-" and r["Init"] == "")
                no_cpu_lim = sum(1 for r in all_rows if r["CPU Lim"] == "-" and r["Init"] == "")
                no_mem_lim = sum(1 for r in all_rows if r["Mem Lim"] == "-" and r["Init"] == "")
                non_init = sum(1 for r in all_rows if r["Init"] == "")
                sc1, sc2, sc3, sc4 = st.columns(4)
                with sc1:
                    st.metric("No CPU Request", f"{no_cpu_req}/{non_init}")
                with sc2:
                    st.metric("No CPU Limit", f"{no_cpu_lim}/{non_init}")
                with sc3:
                    st.metric("No Mem Request", f"{no_mem_req}/{non_init}")
                with sc4:
                    st.metric("No Mem Limit", f"{no_mem_lim}/{non_init}")

                if no_cpu_req > 0 or no_mem_req > 0:
                    st.warning(
                        f"{no_cpu_req + no_mem_req} container(s) are missing resource requests. "
                        "This can affect scheduling and QoS class assignment."
                    )
                if no_cpu_lim > 0 or no_mem_lim > 0:
                    st.info(
                        f"{no_cpu_lim + no_mem_lim} container(s) are missing resource limits. "
                        "Consider setting limits to prevent resource contention."
                    )

                # Download as TSV
                tsv_lines = ["Type\tWorkload\tContainer\tInit\tCPU Req\tCPU Lim\tMem Req\tMem Lim\tEph Req\tEph Lim"]
                for r in all_rows:
                    tsv_lines.append(
                        f"{r['Type']}\t{r['Workload']}\t{r['Container']}\t{r['Init']}\t"
                        f"{r['CPU Req']}\t{r['CPU Lim']}\t{r['Mem Req']}\t{r['Mem Lim']}\t"
                        f"{r['Eph Req']}\t{r['Eph Lim']}"
                    )
                st.download_button(
                    "Download as TSV",
                    data="\n".join(tsv_lines),
                    file_name=f"resource_limits_{rl_ns}.tsv",
                    mime="text/tab-separated-values",
                    key="rl_download",
                )
            else:
                st.info(f"No containers found in namespace `{rl_ns}` for the selected workload type(s).")

    # ── Node Containers (crictl) ────────────────────────────────────────
    with tab_crictl:
        st.markdown("### Node Containers (crictl)")
        st.markdown("View containers running on each node using `crictl ps -a`.")

        if profile.cluster_source == "imported":
            # Imported clusters — no SSH, but we can still get node list and show
            # container info via kubectl debug or just list pods per node
            st.info(
                "**crictl** requires SSH access to each node and is only available for "
                "provisioned clusters. For imported clusters, pod and container "
                "information per node is shown via `kubectl` below."
            )
            st.markdown(
                "This view uses `kubectl get pods --field-selector spec.nodeName=<node>` "
                "to list pods/containers on each node. For full container-level details "
                "(container IDs, image digests, runtime state), SSH into the node and run "
                "`sudo crictl ps -a` directly."
            )
            if st.button("Show containers per node (kubectl)", type="primary", key="crictl_kubectl"):
                with st.spinner("Fetching node list..."):
                    node_result = run_kubectl(
                        profile,
                        "get nodes -o jsonpath='{range .items[*]}{.metadata.name}{\"\\n\"}{end}'",
                        timeout=15,
                    )
                if node_result.success and node_result.stdout.strip():
                    node_names = [n.strip() for n in node_result.stdout.strip().split("\n") if n.strip()]
                    for node_name in node_names:
                        with st.expander(f"Node: **{node_name}**", expanded=True):
                            with st.spinner(f"Fetching containers on {node_name}..."):
                                pod_result = run_kubectl(
                                    profile,
                                    f"get pods -A --field-selector spec.nodeName={node_name} "
                                    "-o custom-columns="
                                    "'NAMESPACE:.metadata.namespace,"
                                    "POD:.metadata.name,"
                                    "CONTAINERS:.spec.containers[*].name,"
                                    "STATUS:.status.phase,"
                                    "RESTARTS:.status.containerStatuses[0].restartCount,"
                                    "NODE:.spec.nodeName'",
                                    timeout=15,
                                )
                                if pod_result.success:
                                    st.code(pod_result.stdout or "(no pods on this node)", language="text")
                                else:
                                    st.error(f"Failed to get pods on {node_name}")
                                    st.code(pod_result.stderr, language="text")
                else:
                    st.error("Failed to list nodes")
                    if node_result.stderr:
                        st.code(node_result.stderr, language="text")
        else:
            # Provisioned clusters — SSH into each node and run crictl
            all_nodes = profile.nodes
            if not all_nodes:
                st.warning("No nodes defined in this profile.")
            else:
                st.markdown(
                    "> **Note:** `crictl` typically requires **root/sudo** access. "
                    "If your SSH user is not root, the command will be prefixed with `sudo`."
                )
                use_sudo = st.checkbox(
                    "Run with sudo (required if SSH user is not root)",
                    value=True,
                    key="crictl_sudo",
                    help="Prefix the command with 'sudo' for non-root SSH users.",
                )
                crictl_cmd = st.text_input(
                    "CRI command",
                    value="crictl ps -a",
                    key="crictl_cmd",
                    help="Command to run on each node (e.g. crictl ps -a, crictl images, crictl stats)",
                )

                crictl_presets = st.columns(5)
                with crictl_presets[0]:
                    if st.button("crictl ps -a", key="cp_ps"):
                        crictl_cmd = "crictl ps -a"
                with crictl_presets[1]:
                    if st.button("crictl images", key="cp_img"):
                        crictl_cmd = "crictl images"
                with crictl_presets[2]:
                    if st.button("crictl stats", key="cp_stats"):
                        crictl_cmd = "crictl stats"
                with crictl_presets[3]:
                    if st.button("crictl pods", key="cp_pods"):
                        crictl_cmd = "crictl pods"
                with crictl_presets[4]:
                    if st.button("crictl info", key="cp_info"):
                        crictl_cmd = "crictl info"

                # Node selection
                node_labels = [
                    f"{n.get('hostname', n.get('ip_address', '?'))} ({n.get('ip_address', '?')}) [{n.get('role', '?')}]"
                    for n in all_nodes
                ]
                cr_select_all = st.checkbox("Run on all nodes", value=True, key="cr_all")

                if not cr_select_all:
                    selected_nodes_idx = st.multiselect(
                        "Select nodes",
                        options=list(range(len(all_nodes))),
                        format_func=lambda i: node_labels[i],
                        default=list(range(len(all_nodes))),
                        key="cr_nodes",
                    )
                    selected_nodes = [all_nodes[i] for i in selected_nodes_idx]
                else:
                    selected_nodes = all_nodes

                if st.button("Run on selected nodes", type="primary", key="crictl_run"):
                    if not selected_nodes:
                        st.warning("No nodes selected. Please select at least one node.")
                    else:
                        actual_cmd = f"sudo {crictl_cmd}" if use_sudo and not crictl_cmd.strip().startswith("sudo") else crictl_cmd
                        all_success = True
                        for node in selected_nodes:
                            node_label = f"{node.get('hostname', node.get('ip_address', '?'))} ({node.get('ip_address', '')})"
                            with st.expander(f"Node: **{node_label}** [{node.get('role', '')}]", expanded=True):
                                with st.spinner(f"Running `{actual_cmd}` on {node_label}..."):
                                    result = run_ssh_command(
                                        ip_address=node["ip_address"],
                                        command=actual_cmd,
                                        ssh_user=node.get("ssh_user", "root"),
                                        ssh_port=node.get("ssh_port", 22),
                                        ssh_key_path=node.get("ssh_key_path", "~/.ssh/id_rsa"),
                                        timeout=30,
                                    )
                                    if result.success:
                                        st.code(result.stdout or "(no output)", language="text")
                                    else:
                                        all_success = False
                                        st.error(f"Command failed on {node_label}")
                                        st.code(result.stderr, language="text")
                                        if "permission denied" in (result.stderr or "").lower():
                                            st.info("Tip: Enable the 'Run with sudo' checkbox above if your SSH user needs elevated privileges.")
                        if all_success:
                            st.success(f"Command completed successfully on {len(selected_nodes)} node(s).")
                        else:
                            st.warning("Command failed on some nodes. Check the details above.")

    # ── Node Health ──────────────────────────────────────────────────────
    with tab_node_health:
        st.markdown("### Node Health Overview")
        st.markdown("View node status, resource usage, and conditions.")

        if st.button("Refresh Node Health", type="primary", key="node_health"):
            col_status, col_top = st.columns(2)

            with col_status:
                st.markdown("#### Node Status")
                with st.spinner("Fetching nodes..."):
                    result = run_kubectl(profile, "get nodes -o wide", timeout=15)
                    if result.success:
                        st.code(result.stdout, language="text")
                    else:
                        st.error("Failed to get nodes")
                        st.code(result.stderr, language="text")

            with col_top:
                st.markdown("#### Resource Usage")
                with st.spinner("Fetching node metrics..."):
                    result = run_kubectl(profile, "top nodes", timeout=15)
                    if result.success:
                        st.code(result.stdout, language="text")
                    else:
                        st.warning("kubectl top requires metrics-server to be installed.")
                        st.code(result.stderr, language="text")

            st.markdown("---")
            st.markdown("#### Node Conditions")
            with st.spinner("Checking node conditions..."):
                result = run_kubectl(
                    profile,
                    'get nodes -o custom-columns='
                    '"NAME:.metadata.name,'
                    'READY:.status.conditions[?(@.type==\\"Ready\\")].status,'
                    'DISK:.status.conditions[?(@.type==\\"DiskPressure\\")].status,'
                    'MEMORY:.status.conditions[?(@.type==\\"MemoryPressure\\")].status,'
                    'PID:.status.conditions[?(@.type==\\"PIDPressure\\")].status"',
                    timeout=15,
                )
                if result.success:
                    st.code(result.stdout, language="text")
                else:
                    st.code(result.stderr, language="text")

            st.markdown("#### Pod Distribution per Node")
            with st.spinner("Fetching pod distribution..."):
                result = run_kubectl(
                    profile,
                    'get pods -A -o custom-columns='
                    '"NODE:.spec.nodeName,NAMESPACE:.metadata.namespace,'
                    'POD:.metadata.name,STATUS:.status.phase" '
                    '--sort-by=.spec.nodeName',
                    timeout=15,
                )
                if result.success:
                    st.code(result.stdout, language="text")
                else:
                    st.code(result.stderr, language="text")

    # ── RBAC Viewer ──────────────────────────────────────────────────────
    with tab_rbac:
        st.markdown("### RBAC Viewer")
        st.markdown("Browse Roles, ClusterRoles, Bindings, and ServiceAccounts.")

        rbac_type = st.selectbox(
            "RBAC Resource",
            [
                "ClusterRoles",
                "ClusterRoleBindings",
                "Roles (namespaced)",
                "RoleBindings (namespaced)",
                "ServiceAccounts",
            ],
            key="rbac_type",
        )

        rbac_ns = ""
        if "(namespaced)" in rbac_type or rbac_type == "ServiceAccounts":
            if _rv_namespaces:
                rbac_ns = st.selectbox(
                    "Namespace (blank = all)",
                    options=[""] + _rv_namespaces,
                    index=0,
                    key="rbac_ns",
                    format_func=lambda x: "All Namespaces" if x == "" else x,
                )
            else:
                rbac_ns = st.text_input(
                    "Namespace",
                    value="default",
                    key="rbac_ns",
                    help="Leave blank for all namespaces",
                )

        if st.button("Fetch RBAC Resources", type="primary", key="fetch_rbac"):
            cmd_map = {
                "ClusterRoles": "get clusterroles",
                "ClusterRoleBindings": "get clusterrolebindings",
                "Roles (namespaced)": "get roles",
                "RoleBindings (namespaced)": "get rolebindings",
                "ServiceAccounts": "get serviceaccounts",
            }
            rbac_cmd = cmd_map[rbac_type]
            if rbac_ns:
                rbac_cmd += f" -n {rbac_ns}"
            elif "(namespaced)" in rbac_type or rbac_type == "ServiceAccounts":
                rbac_cmd += " -A"

            with st.spinner(f"Fetching {rbac_type}..."):
                result = run_kubectl(profile, rbac_cmd, timeout=15)
                if result.success:
                    st.code(result.stdout or "(none found)", language="text")
                else:
                    st.error("Failed to fetch RBAC resources")
                    st.code(result.stderr, language="text")

        # Describe a specific RBAC resource
        st.markdown("---")
        st.markdown("#### Inspect RBAC Resource")
        rbac_name = st.text_input(
            "Resource name to describe",
            placeholder="e.g., cluster-admin",
            key="rbac_desc_name",
        )
        if st.button("Describe RBAC", key="desc_rbac") and rbac_name:
            type_map = {
                "ClusterRoles": "clusterrole",
                "ClusterRoleBindings": "clusterrolebinding",
                "Roles (namespaced)": "role",
                "RoleBindings (namespaced)": "rolebinding",
                "ServiceAccounts": "serviceaccount",
            }
            desc_cmd = f"describe {type_map[rbac_type]} {rbac_name}"
            if rbac_ns:
                desc_cmd += f" -n {rbac_ns}"

            with st.spinner(f"Describing {rbac_name}..."):
                result = run_kubectl(profile, desc_cmd, timeout=15)
                if result.success:
                    st.code(result.stdout, language="yaml")
                else:
                    st.error("Describe failed")
                    st.code(result.stderr, language="text")

    # ── Helm Releases ────────────────────────────────────────────────────
    with tab_helm:
        st.markdown("### Helm Release Manager")
        st.markdown("List, inspect, and manage Helm releases on your cluster.")

        helm_tab_list, helm_tab_install, helm_tab_history = st.tabs([
            "List Releases", "Install Chart", "Release History",
        ])

        with helm_tab_list:
            helm_ns_all = st.checkbox("All namespaces", value=True, key="helm_ns_all")
            helm_ns = ""
            if not helm_ns_all:
                if _rv_namespaces:
                    helm_ns = st.selectbox("Namespace", options=_rv_namespaces,
                                           index=_rv_namespaces.index("default") if "default" in _rv_namespaces else 0,
                                           key="helm_ns")
                else:
                    helm_ns = st.text_input("Namespace", value="default", key="helm_ns")

            if st.button("List Helm Releases", type="primary", key="helm_list"):
                helm_cmd = "helm list"
                if helm_ns_all:
                    helm_cmd += " -A"
                elif helm_ns:
                    helm_cmd += f" -n {helm_ns}"
                helm_cmd += " -o table"

                with st.spinner("Fetching Helm releases..."):
                    result = run_kubectl(profile, helm_cmd.replace("kubectl ", ""), timeout=15)
                    if result.success:
                        st.code(result.stdout or "(no releases found)", language="text")
                    else:
                        st.warning("Helm may not be installed on this cluster.")
                        st.code(result.stderr, language="text")

        with helm_tab_install:
            st.markdown("#### Install a Helm Chart")
            hcol1, hcol2 = st.columns(2)
            with hcol1:
                helm_release_name = st.text_input("Release Name", placeholder="my-release", key="helm_rel")
                helm_chart = st.text_input("Chart", placeholder="prometheus-community/kube-prometheus-stack", key="helm_chart")
            with hcol2:
                if _rv_namespaces:
                    helm_install_ns = st.selectbox("Namespace", options=_rv_namespaces,
                                                   index=_rv_namespaces.index("default") if "default" in _rv_namespaces else 0,
                                                   key="helm_install_ns")
                else:
                    helm_install_ns = st.text_input("Namespace", value="default", key="helm_install_ns")
                helm_create_ns = st.checkbox("Create namespace if not exists", value=True, key="helm_create_ns")
            helm_values = st.text_area(
                "Values (YAML, optional)",
                placeholder="# Custom values.yaml content here",
                height=150,
                key="helm_values",
            )

            if st.button("Install Chart", type="primary", key="helm_install") and helm_release_name and helm_chart:
                install_cmd = f"helm install {helm_release_name} {helm_chart} -n {helm_install_ns}"
                if helm_create_ns:
                    install_cmd += " --create-namespace"
                # If user provided values, write to temp file
                if helm_values.strip():
                    values_path = os.path.join(config.UPLOADS_DIR, f"helm-values-{helm_release_name}.yaml")
                    with open(values_path, "w") as vf:
                        vf.write(helm_values)
                    install_cmd += f" -f {values_path}"

                with st.spinner(f"Installing {helm_chart}..."):
                    result = run_kubectl(profile, install_cmd.replace("kubectl ", ""), timeout=120)
                    if result.success:
                        st.success(f"Release '{helm_release_name}' installed!")
                        st.code(result.stdout, language="text")
                    else:
                        st.error("Helm install failed")
                        st.code(result.stderr, language="text")

        with helm_tab_history:
            st.markdown("#### Release History")
            hist_name = st.text_input("Release name", placeholder="my-release", key="helm_hist_name")
            hist_ns = st.text_input("Namespace", value="default", key="helm_hist_ns")

            if st.button("Get History", key="helm_hist") and hist_name:
                hist_cmd = f"helm history {hist_name} -n {hist_ns}"
                with st.spinner("Fetching history..."):
                    result = run_kubectl(profile, hist_cmd.replace("kubectl ", ""), timeout=15)
                    if result.success:
                        st.code(result.stdout, language="text")
                    else:
                        st.error("Could not get release history")
                        st.code(result.stderr, language="text")

            st.markdown("---")
            st.markdown("#### Rollback Release")
            rb_name = st.text_input("Release name", placeholder="my-release", key="helm_rb_name")
            rb_ns = st.text_input("Namespace", value="default", key="helm_rb_ns")
            rb_rev = st.number_input("Revision number", min_value=1, value=1, key="helm_rb_rev")

            if st.button("Rollback", key="helm_rollback") and rb_name:
                rb_cmd = f"helm rollback {rb_name} {rb_rev} -n {rb_ns}"
                with st.spinner(f"Rolling back {rb_name} to revision {rb_rev}..."):
                    result = run_kubectl(profile, rb_cmd.replace("kubectl ", ""), timeout=60)
                    if result.success:
                        st.success(f"Rolled back '{rb_name}' to revision {rb_rev}")
                        st.code(result.stdout, language="text")
                    else:
                        st.error("Rollback failed")
                        st.code(result.stderr, language="text")

    # ── Events Timeline ──────────────────────────────────────────────────
    with tab_events:
        st.markdown("### Cluster Events Timeline")
        st.markdown("View recent Kubernetes events with graphical analysis.")

        ev_col1, ev_col2, ev_col3 = st.columns(3)
        with ev_col1:
            ev_ns_all = st.checkbox("All namespaces", value=True, key="ev_ns_all")
            ev_ns = ""
            if not ev_ns_all:
                ev_ns = st.text_input("Namespace", value="default", key="ev_ns")
        with ev_col2:
            ev_type = st.selectbox(
                "Event Type",
                ["All", "Normal", "Warning"],
                key="ev_type",
            )
        with ev_col3:
            ev_sort = st.selectbox(
                "Sort by",
                ["Last Timestamp", "First Timestamp", "Count"],
                key="ev_sort",
            )

        if st.button("Fetch Events", type="primary", key="fetch_events"):
            # Fetch events in JSON for graphical display
            ev_json_cmd = "get events"
            if ev_ns_all:
                ev_json_cmd += " -A"
            elif ev_ns:
                ev_json_cmd += f" -n {ev_ns}"
            if ev_type != "All":
                ev_json_cmd += f" --field-selector type={ev_type}"
            ev_json_cmd += " -o json"

            with st.spinner("Fetching events..."):
                result = run_kubectl(profile, ev_json_cmd, timeout=15)

            if result.success and result.stdout.strip():
                try:
                    events_data = json.loads(result.stdout)
                    items = events_data.get("items", [])

                    if not items:
                        st.info("No events found.")
                    else:
                        # Parse events into structured data
                        ev_records = []
                        for item in items:
                            ev_records.append({
                                "Namespace": item.get("metadata", {}).get("namespace", ""),
                                "Type": item.get("type", ""),
                                "Reason": item.get("reason", ""),
                                "Object": item.get("involvedObject", {}).get("name", ""),
                                "Kind": item.get("involvedObject", {}).get("kind", ""),
                                "Message": (item.get("message", "") or "")[:120],
                                "Count": item.get("count", 1),
                                "Last Seen": item.get("lastTimestamp", item.get("eventTime", "")),
                            })

                        import pandas as pd

                        df = pd.DataFrame(ev_records)

                        # ── Graphical Summary ────────────────────────
                        st.markdown("#### Event Summary Charts")

                        chart_col1, chart_col2 = st.columns(2)

                        with chart_col1:
                            st.markdown("**Events by Type**")
                            type_counts = df["Type"].value_counts().reset_index()
                            type_counts.columns = ["Type", "Count"]
                            st.bar_chart(type_counts.set_index("Type"))

                        with chart_col2:
                            st.markdown("**Events by Reason (Top 10)**")
                            reason_counts = df["Reason"].value_counts().head(10).reset_index()
                            reason_counts.columns = ["Reason", "Count"]
                            st.bar_chart(reason_counts.set_index("Reason"))

                        chart_col3, chart_col4 = st.columns(2)

                        with chart_col3:
                            st.markdown("**Events by Namespace (Top 10)**")
                            ns_counts = df["Namespace"].value_counts().head(10).reset_index()
                            ns_counts.columns = ["Namespace", "Count"]
                            st.bar_chart(ns_counts.set_index("Namespace"))

                        with chart_col4:
                            st.markdown("**Events by Object Kind**")
                            kind_counts = df["Kind"].value_counts().reset_index()
                            kind_counts.columns = ["Kind", "Count"]
                            st.bar_chart(kind_counts.set_index("Kind"))

                        # ── Timeline Chart ────────────────────────────
                        st.markdown("---")
                        st.markdown("#### Event Timeline")
                        if df["Last Seen"].notna().any() and df["Last Seen"].str.strip().any():
                            try:
                                df["Timestamp"] = pd.to_datetime(
                                    df["Last Seen"], errors="coerce", utc=True,
                                )
                                ts_df = df.dropna(subset=["Timestamp"])
                                if not ts_df.empty:
                                    ts_df = ts_df.set_index("Timestamp")
                                    # Events over time grouped by type
                                    timeline = ts_df.groupby(
                                        [pd.Grouper(freq="1min"), "Type"]
                                    ).size().unstack(fill_value=0)
                                    if not timeline.empty:
                                        st.line_chart(timeline)
                                    else:
                                        st.info("Not enough timestamp data for timeline chart.")
                                else:
                                    st.info("Could not parse event timestamps for timeline.")
                            except Exception:
                                st.info("Could not render timeline chart from event data.")
                        else:
                            st.info("No timestamp data available for timeline chart.")

                        # ── High-Count Events ─────────────────────────
                        st.markdown("---")
                        st.markdown("#### High-Frequency Events")
                        high_count = df[df["Count"] > 1].sort_values("Count", ascending=False).head(20)
                        if not high_count.empty:
                            st.dataframe(
                                high_count[["Namespace", "Type", "Reason", "Object", "Count", "Message"]],
                                use_container_width=True,
                                hide_index=True,
                            )
                        else:
                            st.info("No repeated events found.")

                        # ── Full Events Table ─────────────────────────
                        st.markdown("---")
                        st.markdown("#### All Events")
                        st.dataframe(df, use_container_width=True, hide_index=True)

                except (json.JSONDecodeError, KeyError):
                    # Fallback to text display
                    st.code(result.stdout, language="text")
            elif result.success:
                st.info("No events found.")
            else:
                st.error("Failed to fetch events")
                st.code(result.stderr, language="text")

        # Warning events summary
        st.markdown("---")
        st.markdown("#### Warning Events Summary")
        if st.button("Show Warning Events", key="warn_events"):
            warn_cmd = (
                "get events -A --field-selector type=Warning "
                "-o custom-columns="
                "'NAMESPACE:.metadata.namespace,"
                "LAST_SEEN:.lastTimestamp,"
                "COUNT:.count,"
                "REASON:.reason,"
                "OBJECT:.involvedObject.name,"
                "MESSAGE:.message' "
                "--sort-by=.lastTimestamp"
            )
            with st.spinner("Fetching warning events..."):
                result = run_kubectl(profile, warn_cmd, timeout=15)
                if result.success:
                    st.code(result.stdout or "(no warning events)", language="text")
                else:
                    st.code(result.stderr, language="text")

    # ── Pod Restart Tracker ───────────────────────────────────────────────
    with tab_restart_tracker:
        st.markdown("### Pod Restart Tracker")
        st.markdown("Identify pods with frequent restarts, OOMKilled containers, and CrashLoopBackOff issues.")

        rcol1, rcol2 = st.columns([2, 1])
        with rcol1:
            if _rv_namespaces:
                restart_ns = st.selectbox("Namespace", ["All Namespaces"] + _rv_namespaces, key="restart_ns")
            else:
                restart_ns = st.text_input("Namespace (blank = all)", value="", key="restart_ns_text")
                if not restart_ns:
                    restart_ns = "All Namespaces"
        with rcol2:
            min_restarts = st.number_input("Min restarts to show", min_value=0, value=1, key="min_restarts")

        if st.button("Load Pod Restarts", type="primary", key="load_restarts"):
            ns_flag = "-A" if restart_ns == "All Namespaces" else f"-n {restart_ns}"
            cmd = f"get pods {ns_flag} -o json"
            with st.spinner("Fetching pod data..."):
                result = run_kubectl(profile, cmd, timeout=30)
            if result.success and result.stdout.strip():
                try:
                    import pandas as pd
                    pods_json = json.loads(result.stdout)
                    restart_data = []
                    for pod in pods_json.get("items", []):
                        pod_name = pod.get("metadata", {}).get("name", "?")
                        pod_ns = pod.get("metadata", {}).get("namespace", "?")
                        for cs in pod.get("status", {}).get("containerStatuses", []):
                            restarts = cs.get("restartCount", 0)
                            if restarts < min_restarts:
                                continue
                            container_name = cs.get("name", "?")
                            ready = cs.get("ready", False)
                            # Detect OOMKilled
                            last_state = cs.get("lastState", {})
                            terminated = last_state.get("terminated", {})
                            reason = terminated.get("reason", "")
                            exit_code = terminated.get("exitCode", "")
                            # Current state
                            state = cs.get("state", {})
                            if "running" in state:
                                current_state = "Running"
                            elif "waiting" in state:
                                current_state = state["waiting"].get("reason", "Waiting")
                            elif "terminated" in state:
                                current_state = state["terminated"].get("reason", "Terminated")
                            else:
                                current_state = "Unknown"
                            restart_data.append({
                                "Namespace": pod_ns,
                                "Pod": pod_name,
                                "Container": container_name,
                                "Restarts": restarts,
                                "Ready": ready,
                                "State": current_state,
                                "Last Termination": reason or "N/A",
                                "Exit Code": str(exit_code) if exit_code != "" else "N/A",
                            })
                    if restart_data:
                        df = pd.DataFrame(restart_data).sort_values("Restarts", ascending=False)
                        # Summary metrics
                        total_restarts = df["Restarts"].sum()
                        oom_count = len(df[df["Last Termination"] == "OOMKilled"])
                        crash_count = len(df[df["State"] == "CrashLoopBackOff"])
                        mcol1, mcol2, mcol3, mcol4 = st.columns(4)
                        mcol1.metric("Containers with Restarts", len(df))
                        mcol2.metric("Total Restarts", int(total_restarts))
                        mcol3.metric("OOMKilled", oom_count)
                        mcol4.metric("CrashLoopBackOff", crash_count)
                        st.dataframe(df, use_container_width=True, hide_index=True)
                        # Highlight problematic pods
                        if oom_count > 0:
                            st.warning(
                                f"{oom_count} container(s) were terminated due to **OOMKilled** — "
                                "consider increasing memory limits for those workloads."
                            )
                        if crash_count > 0:
                            st.error(
                                f"{crash_count} container(s) are in **CrashLoopBackOff** — "
                                "check logs with `kubectl logs <pod> -c <container> --previous`."
                            )
                    else:
                        st.success(f"No containers found with {min_restarts}+ restarts. Cluster looks healthy!")
                except (json.JSONDecodeError, KeyError) as e:
                    st.error(f"Failed to parse pod data: {e}")
                    st.code(result.stdout[:2000], language="text")
            elif result.success:
                st.info("No pods found.")
            else:
                st.error("Failed to fetch pods")
                st.code(result.stderr, language="text")

    # ── Network Policy Visualizer ─────────────────────────────────────────
    with tab_netpol:
        st.markdown("### Network Policy Visualizer")
        st.markdown("View and analyze NetworkPolicies to understand pod-to-pod communication rules.")

        npcol1, npcol2 = st.columns([2, 1])
        with npcol1:
            if _rv_namespaces:
                np_ns = st.selectbox("Namespace", ["All Namespaces"] + _rv_namespaces, key="netpol_ns")
            else:
                np_ns = st.text_input("Namespace (blank = all)", value="", key="netpol_ns_text")
                if not np_ns:
                    np_ns = "All Namespaces"

        if st.button("Load Network Policies", type="primary", key="load_netpol"):
            ns_flag = "-A" if np_ns == "All Namespaces" else f"-n {np_ns}"
            cmd = f"get networkpolicies {ns_flag} -o json"
            with st.spinner("Fetching network policies..."):
                result = run_kubectl(profile, cmd, timeout=15)
            if result.success and result.stdout.strip():
                try:
                    import pandas as pd
                    np_json = json.loads(result.stdout)
                    policies = np_json.get("items", [])
                    if not policies:
                        st.info("No NetworkPolicies found. All pod-to-pod traffic is allowed by default.")
                    else:
                        st.markdown(f"**Found {len(policies)} NetworkPolicies**")

                        policy_summary = []
                        for pol in policies:
                            meta = pol.get("metadata", {})
                            spec = pol.get("spec", {})
                            pol_name = meta.get("name", "?")
                            pol_ns = meta.get("namespace", "?")
                            # Pod selector
                            pod_sel = spec.get("podSelector", {})
                            match_labels = pod_sel.get("matchLabels", {})
                            selector_str = ", ".join(f"{k}={v}" for k, v in match_labels.items()) if match_labels else "(all pods)"
                            # Policy types
                            policy_types = spec.get("policyTypes", [])
                            # Ingress rules count
                            ingress_rules = spec.get("ingress", [])
                            egress_rules = spec.get("egress", [])

                            policy_summary.append({
                                "Namespace": pol_ns,
                                "Policy": pol_name,
                                "Pod Selector": selector_str,
                                "Types": ", ".join(policy_types) if policy_types else "N/A",
                                "Ingress Rules": len(ingress_rules),
                                "Egress Rules": len(egress_rules),
                            })

                        st.dataframe(pd.DataFrame(policy_summary), use_container_width=True, hide_index=True)

                        # Detailed view per policy
                        for pol in policies:
                            meta = pol.get("metadata", {})
                            spec = pol.get("spec", {})
                            pol_name = meta.get("name", "?")
                            pol_ns = meta.get("namespace", "?")
                            with st.expander(f"{pol_ns}/{pol_name}", expanded=False):
                                # Pod selector
                                pod_sel = spec.get("podSelector", {})
                                match_labels = pod_sel.get("matchLabels", {})
                                if match_labels:
                                    st.markdown("**Applies to pods matching:** " + ", ".join(f"`{k}={v}`" for k, v in match_labels.items()))
                                else:
                                    st.markdown("**Applies to:** All pods in namespace")

                                # Ingress
                                ingress_rules = spec.get("ingress", [])
                                if ingress_rules:
                                    st.markdown("**Ingress Rules:**")
                                    for i, rule in enumerate(ingress_rules):
                                        sources = []
                                        for fr in rule.get("from", []):
                                            if "podSelector" in fr:
                                                labels = fr["podSelector"].get("matchLabels", {})
                                                sources.append("Pods: " + (", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "all"))
                                            if "namespaceSelector" in fr:
                                                labels = fr["namespaceSelector"].get("matchLabels", {})
                                                sources.append("Namespaces: " + (", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "all"))
                                            if "ipBlock" in fr:
                                                sources.append(f"CIDR: {fr['ipBlock'].get('cidr', '?')}")
                                        ports = []
                                        for p in rule.get("ports", []):
                                            ports.append(f"{p.get('protocol', 'TCP')}/{p.get('port', '*')}")
                                        src_str = ", ".join(sources) if sources else "any"
                                        port_str = ", ".join(ports) if ports else "all ports"
                                        st.markdown(f"  - Rule {i+1}: Allow from **{src_str}** on **{port_str}**")
                                elif "Ingress" in spec.get("policyTypes", []):
                                    st.warning("Ingress type declared but no rules — all ingress traffic is **denied**.")

                                # Egress
                                egress_rules = spec.get("egress", [])
                                if egress_rules:
                                    st.markdown("**Egress Rules:**")
                                    for i, rule in enumerate(egress_rules):
                                        destinations = []
                                        for to in rule.get("to", []):
                                            if "podSelector" in to:
                                                labels = to["podSelector"].get("matchLabels", {})
                                                destinations.append("Pods: " + (", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "all"))
                                            if "namespaceSelector" in to:
                                                labels = to["namespaceSelector"].get("matchLabels", {})
                                                destinations.append("Namespaces: " + (", ".join(f"{k}={v}" for k, v in labels.items()) if labels else "all"))
                                            if "ipBlock" in to:
                                                destinations.append(f"CIDR: {to['ipBlock'].get('cidr', '?')}")
                                        ports = []
                                        for p in rule.get("ports", []):
                                            ports.append(f"{p.get('protocol', 'TCP')}/{p.get('port', '*')}")
                                        dest_str = ", ".join(destinations) if destinations else "any"
                                        port_str = ", ".join(ports) if ports else "all ports"
                                        st.markdown(f"  - Rule {i+1}: Allow to **{dest_str}** on **{port_str}**")
                                elif "Egress" in spec.get("policyTypes", []):
                                    st.warning("Egress type declared but no rules — all egress traffic is **denied**.")

                                st.markdown("---")
                                st.markdown("**Raw YAML:**")
                                import yaml
                                st.code(yaml.dump(pol, default_flow_style=False), language="yaml")

                        # Coverage check
                        st.markdown("---")
                        st.markdown("#### Coverage Analysis")
                        if st.button("Check Unprotected Pods", key="netpol_coverage"):
                            # Get all pods and check which are selected by a policy
                            pod_ns_flag = f"-n {np_ns}" if np_ns != "All Namespaces" else "-A"
                            pod_cmd = f"get pods {pod_ns_flag} -o json"
                            with st.spinner("Analyzing coverage..."):
                                pod_result = run_kubectl(profile, pod_cmd, timeout=15)
                            if pod_result.success and pod_result.stdout.strip():
                                try:
                                    all_pods = json.loads(pod_result.stdout).get("items", [])
                                    protected_pods = set()
                                    for pol in policies:
                                        pol_ns_name = pol.get("metadata", {}).get("namespace", "")
                                        pod_sel = pol.get("spec", {}).get("podSelector", {})
                                        match_labels = pod_sel.get("matchLabels", {})
                                        for p in all_pods:
                                            p_ns = p.get("metadata", {}).get("namespace", "")
                                            p_name = p.get("metadata", {}).get("name", "")
                                            p_labels = p.get("metadata", {}).get("labels", {})
                                            if p_ns != pol_ns_name:
                                                continue
                                            if not match_labels or all(p_labels.get(k) == v for k, v in match_labels.items()):
                                                protected_pods.add(f"{p_ns}/{p_name}")
                                    unprotected = []
                                    for p in all_pods:
                                        p_ns = p.get("metadata", {}).get("namespace", "")
                                        p_name = p.get("metadata", {}).get("name", "")
                                        if f"{p_ns}/{p_name}" not in protected_pods:
                                            unprotected.append({"Namespace": p_ns, "Pod": p_name})
                                    if unprotected:
                                        st.warning(f"{len(unprotected)} pod(s) are **not covered** by any NetworkPolicy (all traffic allowed):")
                                        st.dataframe(pd.DataFrame(unprotected), use_container_width=True, hide_index=True)
                                    else:
                                        st.success("All pods are covered by at least one NetworkPolicy.")
                                except (json.JSONDecodeError, KeyError):
                                    st.error("Failed to parse pod data for coverage analysis.")

                except (json.JSONDecodeError, KeyError) as e:
                    st.error(f"Failed to parse network policy data: {e}")
            elif result.success:
                st.info("No NetworkPolicies found. All pod-to-pod traffic is allowed by default.")
            else:
                st.error("Failed to fetch network policies")
                st.code(result.stderr, language="text")

    # ── PVC / Storage Dashboard ───────────────────────────────────────────
    with tab_pvc:
        st.markdown("### PVC / Storage Dashboard")
        st.markdown("View PersistentVolumeClaims, PersistentVolumes, and StorageClasses.")

        pvc_sub = st.radio(
            "View",
            ["PVCs", "PersistentVolumes", "StorageClasses"],
            horizontal=True,
            key="pvc_view",
        )

        if pvc_sub == "PVCs":
            pcol1, pcol2 = st.columns([2, 1])
            with pcol1:
                if _rv_namespaces:
                    pvc_ns = st.selectbox("Namespace", ["All Namespaces"] + _rv_namespaces, key="pvc_ns")
                else:
                    pvc_ns = st.text_input("Namespace (blank = all)", value="", key="pvc_ns_text")
                    if not pvc_ns:
                        pvc_ns = "All Namespaces"

            if st.button("Load PVCs", type="primary", key="load_pvcs"):
                ns_flag = "-A" if pvc_ns == "All Namespaces" else f"-n {pvc_ns}"
                cmd = f"get pvc {ns_flag} -o json"
                with st.spinner("Fetching PVCs..."):
                    result = run_kubectl(profile, cmd, timeout=15)
                if result.success and result.stdout.strip():
                    try:
                        import pandas as pd
                        pvc_json = json.loads(result.stdout)
                        pvcs = pvc_json.get("items", [])
                        if not pvcs:
                            st.info("No PVCs found.")
                        else:
                            pvc_data = []
                            for pvc in pvcs:
                                meta = pvc.get("metadata", {})
                                spec = pvc.get("spec", {})
                                status = pvc.get("status", {})
                                capacity = status.get("capacity", {}).get("storage", "N/A")
                                requested = spec.get("resources", {}).get("requests", {}).get("storage", "N/A")
                                pvc_data.append({
                                    "Namespace": meta.get("namespace", "?"),
                                    "Name": meta.get("name", "?"),
                                    "Status": status.get("phase", "?"),
                                    "Volume": spec.get("volumeName", "N/A"),
                                    "Capacity": capacity,
                                    "Requested": requested,
                                    "Access Modes": ", ".join(spec.get("accessModes", [])),
                                    "Storage Class": spec.get("storageClassName", "N/A"),
                                })
                            df = pd.DataFrame(pvc_data)
                            # Summary
                            bound = len(df[df["Status"] == "Bound"])
                            pending = len(df[df["Status"] == "Pending"])
                            lost = len(df[df["Status"] == "Lost"])
                            scol1, scol2, scol3, scol4 = st.columns(4)
                            scol1.metric("Total PVCs", len(df))
                            scol2.metric("Bound", bound)
                            scol3.metric("Pending", pending)
                            scol4.metric("Lost", lost)
                            if pending > 0:
                                st.warning(f"{pending} PVC(s) are **Pending** — check StorageClass availability and provisioner status.")
                            if lost > 0:
                                st.error(f"{lost} PVC(s) are **Lost** — the bound PV has been deleted. Data may be lost.")
                            st.dataframe(df, use_container_width=True, hide_index=True)
                    except (json.JSONDecodeError, KeyError) as e:
                        st.error(f"Failed to parse PVC data: {e}")
                elif result.success:
                    st.info("No PVCs found.")
                else:
                    st.error("Failed to fetch PVCs")
                    st.code(result.stderr, language="text")

        elif pvc_sub == "PersistentVolumes":
            if st.button("Load PVs", type="primary", key="load_pvs"):
                cmd = "get pv -o json"
                with st.spinner("Fetching PersistentVolumes..."):
                    result = run_kubectl(profile, cmd, timeout=15)
                if result.success and result.stdout.strip():
                    try:
                        import pandas as pd
                        pv_json = json.loads(result.stdout)
                        pvs = pv_json.get("items", [])
                        if not pvs:
                            st.info("No PersistentVolumes found.")
                        else:
                            pv_data = []
                            for pv in pvs:
                                meta = pv.get("metadata", {})
                                spec = pv.get("spec", {})
                                status = pv.get("status", {})
                                claim_ref = spec.get("claimRef", {})
                                claim = f"{claim_ref.get('namespace', '')}/{claim_ref.get('name', '')}" if claim_ref else "Unbound"
                                pv_data.append({
                                    "Name": meta.get("name", "?"),
                                    "Capacity": spec.get("capacity", {}).get("storage", "N/A"),
                                    "Access Modes": ", ".join(spec.get("accessModes", [])),
                                    "Reclaim Policy": spec.get("persistentVolumeReclaimPolicy", "N/A"),
                                    "Status": status.get("phase", "?"),
                                    "Claim": claim,
                                    "Storage Class": spec.get("storageClassName", "N/A"),
                                    "Volume Mode": spec.get("volumeMode", "N/A"),
                                })
                            df = pd.DataFrame(pv_data)
                            avail = len(df[df["Status"] == "Available"])
                            bound = len(df[df["Status"] == "Bound"])
                            released = len(df[df["Status"] == "Released"])
                            scol1, scol2, scol3, scol4 = st.columns(4)
                            scol1.metric("Total PVs", len(df))
                            scol2.metric("Bound", bound)
                            scol3.metric("Available", avail)
                            scol4.metric("Released", released)
                            st.dataframe(df, use_container_width=True, hide_index=True)
                    except (json.JSONDecodeError, KeyError) as e:
                        st.error(f"Failed to parse PV data: {e}")
                elif result.success:
                    st.info("No PersistentVolumes found.")
                else:
                    st.error("Failed to fetch PVs")
                    st.code(result.stderr, language="text")

        elif pvc_sub == "StorageClasses":
            if st.button("Load Storage Classes", type="primary", key="load_sc"):
                cmd = "get storageclasses -o json"
                with st.spinner("Fetching StorageClasses..."):
                    result = run_kubectl(profile, cmd, timeout=15)
                if result.success and result.stdout.strip():
                    try:
                        import pandas as pd
                        sc_json = json.loads(result.stdout)
                        scs = sc_json.get("items", [])
                        if not scs:
                            st.info("No StorageClasses found.")
                        else:
                            sc_data = []
                            for sc in scs:
                                meta = sc.get("metadata", {})
                                annotations = meta.get("annotations", {})
                                is_default = annotations.get("storageclass.kubernetes.io/is-default-class", "false") == "true"
                                sc_data.append({
                                    "Name": meta.get("name", "?"),
                                    "Provisioner": sc.get("provisioner", "N/A"),
                                    "Reclaim Policy": sc.get("reclaimPolicy", "N/A"),
                                    "Volume Binding": sc.get("volumeBindingMode", "N/A"),
                                    "Allow Expansion": sc.get("allowVolumeExpansion", False),
                                    "Default": is_default,
                                })
                            st.dataframe(pd.DataFrame(sc_data), use_container_width=True, hide_index=True)
                    except (json.JSONDecodeError, KeyError) as e:
                        st.error(f"Failed to parse StorageClass data: {e}")
                elif result.success:
                    st.info("No StorageClasses found.")
                else:
                    st.error("Failed to fetch StorageClasses")
                    st.code(result.stderr, language="text")


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Upgrade Planner
# ══════════════════════════════════════════════════════════════════════════

_K8S_VERSIONS_DETAIL = [
    {
        "version": "1.35",
        "release": "2026-04",
        "end_of_life": "2027-08",
        "highlights": "Sidecar containers GA, improved pod lifecycle management, dynamic resource allocation enhancements.",
    },
    {
        "version": "1.34",
        "release": "2025-12",
        "end_of_life": "2027-04",
        "highlights": "Structured authorization config GA, recursive read-only mounts, traffic distribution improvements.",
    },
    {
        "version": "1.33",
        "release": "2025-08",
        "end_of_life": "2027-01",
        "highlights": "In-place pod resize beta, multi-network pods alpha, nftables kube-proxy backend.",
    },
    {
        "version": "1.32",
        "release": "2025-04",
        "end_of_life": "2026-08",
        "highlights": "Dynamic resource allocation (DRA) beta, auto-remove PV claims, job success policy GA.",
    },
    {
        "version": "1.31",
        "release": "2024-12",
        "end_of_life": "2026-04",
        "highlights": "AppArmor GA, nftables proxy GA, improved ingress connectivity reliability, cgroup v2 enhancements.",
    },
    {
        "version": "1.30",
        "release": "2024-04",
        "end_of_life": "2025-08",
        "highlights": "Contextual logging GA, CEL admission improvements, pod scheduling readiness.",
    },
    {
        "version": "1.29",
        "release": "2023-12",
        "end_of_life": "2025-02",
        "highlights": "KMS v2 GA, ReadWriteOncePod GA, networking improvements, node memory manager.",
    },
    {
        "version": "1.28",
        "release": "2023-08",
        "end_of_life": "2024-10",
        "highlights": "Sidecar containers alpha, recovery from non-graceful node shutdown, mixed version proxy.",
    },
    {
        "version": "1.27",
        "release": "2023-04",
        "end_of_life": "2024-06",
        "highlights": "In-place pod resize alpha, VPA improvements, SeccompDefault GA.",
    },
]


def page_upgrade_planner():
    st.markdown("## Upgrade Planner")
    st.markdown("Plan and prepare Kubernetes version upgrades for your cluster.")

    profile = _get_active_profile()
    if not profile:
        return

    current_ver = profile.kubernetes_version

    tab_overview, tab_preflight, tab_plan, tab_changelog = st.tabs([
        "Version Overview",
        "Pre-flight Checks",
        "Upgrade Steps",
        "Changelog & Compatibility",
    ])

    # ── Version Overview ─────────────────────────────────────────────────
    with tab_overview:
        st.markdown("### Kubernetes Version Matrix")
        st.info(f"Your current cluster version: **{current_ver}**")

        # Build a table
        rows = []
        for v in _K8S_VERSIONS_DETAIL:
            status = ""
            if v["version"] == current_ver:
                status = "CURRENT"
            elif v["version"] > current_ver:
                status = "UPGRADE AVAILABLE"
            else:
                status = "OLDER"
            rows.append({
                "Version": v["version"],
                "Status": status,
                "Release Date": v["release"],
                "End of Life": v["end_of_life"],
                "Highlights": v["highlights"],
            })

        st.dataframe(rows, use_container_width=True, hide_index=True)

        # Upgrade target selection
        st.markdown("---")
        available_upgrades = [
            v["version"] for v in _K8S_VERSIONS_DETAIL if v["version"] > current_ver
        ]
        if available_upgrades:
            target_version = st.selectbox(
                "Select target upgrade version",
                available_upgrades,
                key="upgrade_target",
            )
            skipped = [
                v for v in _K8S_VERSIONS_DETAIL
                if current_ver < v["version"] <= target_version
            ]
            if len(skipped) > 1:
                st.warning(
                    f"You are skipping {len(skipped) - 1} minor version(s). "
                    "Kubernetes supports upgrading one minor version at a time. "
                    "Plan incremental upgrades for production clusters."
                )
            st.markdown("#### Upgrade Path")
            path_versions = [current_ver] + [v["version"] for v in reversed(skipped)]
            st.markdown(" → ".join([f"**{v}**" for v in path_versions]))
        else:
            st.success("You are running the latest version!")

    # ── Pre-flight Checks ────────────────────────────────────────────────
    with tab_preflight:
        st.markdown("### Pre-Upgrade Checks")
        st.markdown("Run these checks before starting the upgrade process.")

        checks = [
            ("Cluster Health", "get nodes -o wide"),
            ("All Pods Running", "get pods -A --field-selector 'status.phase!=Running,status.phase!=Succeeded'"),
            ("etcd Health", "get --raw=/healthz"),
            ("API Server Version", "version"),
            ("PodDisruptionBudgets", "get pdb -A"),
            ("Deprecated APIs", "api-resources --api-group=extensions"),
            ("Persistent Volumes", "get pv"),
            ("Component Statuses", "get cs 2>/dev/null || echo 'Deprecated in newer versions'"),
        ]

        if st.button("Run All Pre-flight Checks", type="primary", key="preflight"):
            all_ok = True
            for name, cmd in checks:
                with st.status(f"Checking: {name}...", expanded=False) as status:
                    result = run_kubectl(profile, cmd, timeout=15)
                    if result.success:
                        st.code(result.stdout or "(no output)", language="text")
                        status.update(label=f"{name} — OK", state="complete")
                    else:
                        st.code(result.stderr, language="text")
                        status.update(label=f"{name} — ISSUE", state="error")
                        all_ok = False

            if all_ok:
                st.success("All pre-flight checks passed! The cluster looks ready for upgrade.")
            else:
                st.warning(
                    "Some checks reported issues. Review the output above before proceeding."
                )

        st.markdown("---")
        st.markdown("#### Backup Checklist")
        st.markdown(
            "Before upgrading, ensure you have:\n\n"
            "- [ ] **etcd snapshot backup**: `ETCDCTL_API=3 etcdctl snapshot save /backup/etcd-snapshot.db`\n"
            "- [ ] **Cluster state export**: `kubectl get all -A -o yaml > cluster-backup.yaml`\n"
            "- [ ] **PV/PVC data backed up** (if applicable)\n"
            "- [ ] **CNI configuration backed up**: `/etc/cni/net.d/`\n"
            "- [ ] **kubeadm config backed up**: `kubeadm config view > kubeadm-config.yaml`\n"
            "- [ ] **VM/node snapshots taken** (if running on VMs)\n"
        )

    # ── Upgrade Steps ────────────────────────────────────────────────────
    with tab_plan:
        st.markdown("### Step-by-Step Upgrade Plan")

        target = st.selectbox(
            "Target Version",
            [v["version"] for v in _K8S_VERSIONS_DETAIL if v["version"] > current_ver] or [current_ver],
            key="upgrade_plan_target",
        )

        st.markdown(f"#### Upgrading from {current_ver} → {target}")

        st.markdown(
            f"""
**Phase 1: Prepare (Control Plane)**
```bash
# 1. Update package repositories
sudo apt-get update

# 2. Check available kubeadm versions
apt-cache madison kubeadm | grep {target}

# 3. Upgrade kubeadm
sudo apt-mark unhold kubeadm
sudo apt-get install -y kubeadm={target}.*
sudo apt-mark hold kubeadm

# 4. Verify kubeadm version
kubeadm version

# 5. Check upgrade plan
sudo kubeadm upgrade plan
```

**Phase 2: Upgrade Control Plane**
```bash
# 1. Drain the control-plane node
kubectl drain <cp-node> --ignore-daemonsets --delete-emptydir-data

# 2. Apply the upgrade
sudo kubeadm upgrade apply v{target}.0

# 3. Upgrade kubelet & kubectl
sudo apt-mark unhold kubelet kubectl
sudo apt-get install -y kubelet={target}.* kubectl={target}.*
sudo apt-mark hold kubelet kubectl

# 4. Restart kubelet
sudo systemctl daemon-reload
sudo systemctl restart kubelet

# 5. Uncordon the node
kubectl uncordon <cp-node>
```

**Phase 3: Upgrade Worker Nodes** (repeat for each worker)
```bash
# On each worker node:
# 1. Drain the worker
kubectl drain <worker-node> --ignore-daemonsets --delete-emptydir-data

# 2. Upgrade kubeadm, kubelet, kubectl
sudo apt-mark unhold kubeadm kubelet kubectl
sudo apt-get install -y kubeadm={target}.* kubelet={target}.* kubectl={target}.*
sudo apt-mark hold kubeadm kubelet kubectl

# 3. Upgrade node config
sudo kubeadm upgrade node

# 4. Restart kubelet
sudo systemctl daemon-reload
sudo systemctl restart kubelet

# 5. Uncordon
kubectl uncordon <worker-node>
```

**Phase 4: Upgrade CRI-O** (on each node)
```bash
# Update CRI-O to match the K8s version
sudo apt-get install -y cri-o={target}.*
sudo systemctl restart crio
sudo systemctl restart kubelet
```

**Phase 5: Verify**
```bash
kubectl get nodes -o wide
kubectl get pods -A
kubectl version
```
"""
        )

    # ── Changelog & Compatibility ────────────────────────────────────────
    with tab_changelog:
        st.markdown("### Version Changelog & Compatibility Notes")

        for v in _K8S_VERSIONS_DETAIL:
            marker = " ← CURRENT" if v["version"] == current_ver else ""
            with st.expander(f"Kubernetes {v['version']}{marker}", expanded=(v["version"] == current_ver)):
                st.markdown(f"**Release Date:** {v['release']}")
                st.markdown(f"**End of Life:** {v['end_of_life']}")
                st.markdown(f"**Key Highlights:** {v['highlights']}")
                st.markdown("---")
                st.markdown(
                    f"**Compatibility:**\n"
                    f"- CRI-O: {v['version']}.x\n"
                    f"- Flannel: Compatible (check release notes for CNI spec changes)\n"
                    f"- etcd: 3.5.x+ recommended\n"
                    f"- CoreDNS: 1.11.x+ recommended\n"
                )
                st.markdown(
                    f"**Upgrade Notes:**\n"
                    f"- Always upgrade one minor version at a time\n"
                    f"- Check deprecated API versions before upgrading\n"
                    f"- Run `kubeadm upgrade plan` to verify compatibility\n"
                    f"- Back up etcd before starting\n"
                )


def page_ai_assistant():
    st.markdown("## AI Kubernetes Assistant")

    if not is_llm_configured():
        st.info(
            "LLM is not configured. Set `LLM_API_URL` and `LLM_API_KEY` "
            "environment variables to enable the AI chat assistant."
        )
        st.markdown(
            "All other features (Cluster Creation, Debugging, Monitoring, Log Analysis) "
            "work without an LLM. Only the AI-powered analysis and chat features require it."
        )
        return

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


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Multi-Cluster Dashboard
# ══════════════════════════════════════════════════════════════════════════

def page_multi_cluster_dashboard():
    st.markdown("## Multi-Cluster Dashboard")
    st.markdown("Overview of all registered cluster profiles at a glance.")

    profiles = list_profiles()
    if not profiles:
        st.info("No cluster profiles yet. Create one in the **Profile Manager** or import a cluster via kubeconfig.")
        return

    # Summary metrics
    total = len(profiles)
    imported = sum(1 for p in profiles if p.cluster_source == "imported")
    provisioned = total - imported
    active_count = sum(1 for p in profiles if p.status == "active")
    draft_count = sum(1 for p in profiles if p.status == "draft")
    error_count = sum(1 for p in profiles if p.status == "error")

    mcol1, mcol2, mcol3, mcol4, mcol5 = st.columns(5)
    mcol1.metric("Total Clusters", total)
    mcol2.metric("Provisioned", provisioned)
    mcol3.metric("Imported", imported)
    mcol4.metric("Active", active_count)
    mcol5.metric("Errors", error_count)

    st.markdown("---")

    # Cluster cards
    for profile in profiles:
        status_icon = {"active": "🟢", "error": "🔴", "draft": "⚪", "provisioning": "🟡"}.get(profile.status, "⚪")
        source_label = "Imported" if profile.cluster_source == "imported" else "Provisioned"

        with st.expander(
            f"{status_icon} **{profile.name}** — {source_label} | {profile.status.upper()}",
            expanded=(profile.status == "error"),
        ):
            col1, col2, col3 = st.columns(3)
            with col1:
                st.markdown(f"**K8s Version:** {profile.kubernetes_version}")
                st.markdown(f"**Source:** {source_label}")
                st.markdown(f"**Status:** {profile.status.upper()}")
            with col2:
                if profile.cluster_source == "imported":
                    st.markdown(f"**Kubeconfig:** {'Loaded' if profile.kubeconfig_content else 'Not loaded'}")
                else:
                    cp = len(profile.get_control_plane_nodes())
                    wk = len(profile.get_worker_nodes())
                    st.markdown(f"**Nodes:** {cp} control-plane + {wk} worker")
                    st.markdown(f"**CRI-O:** {profile.crio_version}")
                    st.markdown(f"**CNI:** Flannel")
            with col3:
                if profile.description:
                    st.markdown(f"**Description:** {profile.description}")

            # Live cluster health check for imported clusters
            if profile.cluster_source == "imported" and profile.kubeconfig_content:
                if st.button(f"Check Health", key=f"health_{profile.name}"):
                    with st.spinner("Checking cluster health..."):
                        node_result = run_kubectl(profile, "get nodes --no-headers", timeout=10)
                        if node_result.success and node_result.stdout.strip():
                            lines = [l for l in node_result.stdout.strip().split("\n") if l.strip()]
                            total_nodes = len(lines)
                            ready_nodes = sum(1 for l in lines if "Ready" in l.split()[1] if len(l.split()) > 1)
                            not_ready = total_nodes - ready_nodes
                            hcol1, hcol2, hcol3 = st.columns(3)
                            hcol1.metric("Nodes", total_nodes)
                            hcol2.metric("Ready", ready_nodes)
                            hcol3.metric("Not Ready", not_ready)
                            if not_ready > 0:
                                st.warning(f"{not_ready} node(s) are not Ready.")
                            else:
                                st.success("All nodes are Ready.")
                            # Pod summary
                            pod_result = run_kubectl(profile, "get pods -A --no-headers", timeout=15)
                            if pod_result.success and pod_result.stdout.strip():
                                pod_lines = [l for l in pod_result.stdout.strip().split("\n") if l.strip()]
                                total_pods = len(pod_lines)
                                running_pods = sum(1 for l in pod_lines if "Running" in l)
                                failed_pods = sum(1 for l in pod_lines if any(s in l for s in ["Error", "CrashLoopBackOff", "ImagePullBackOff"]))
                                pcol1, pcol2, pcol3 = st.columns(3)
                                pcol1.metric("Total Pods", total_pods)
                                pcol2.metric("Running", running_pods)
                                pcol3.metric("Failed/Error", failed_pods)
                        elif node_result.success:
                            st.info("Connected but no nodes found.")
                        else:
                            st.error(f"Could not connect: {node_result.stderr or 'kubectl failed'}")

            # Quick actions
            if profile.cluster_source == "imported" and profile.kubeconfig_content:
                qcol1, qcol2, qcol3 = st.columns(3)
                with qcol1:
                    if st.button("View Nodes", key=f"qnodes_{profile.name}"):
                        result = run_kubectl(profile, "get nodes -o wide", timeout=10)
                        if result.success:
                            st.code(result.stdout or "(no output)", language="text")
                        else:
                            st.error(result.stderr or "Failed")
                with qcol2:
                    if st.button("View Namespaces", key=f"qns_{profile.name}"):
                        result = run_kubectl(profile, "get namespaces", timeout=10)
                        if result.success:
                            st.code(result.stdout or "(no output)", language="text")
                        else:
                            st.error(result.stderr or "Failed")
                with qcol3:
                    if st.button("Warning Events", key=f"qevents_{profile.name}"):
                        result = run_kubectl(
                            profile,
                            "get events -A --field-selector type=Warning --sort-by=.lastTimestamp",
                            timeout=15,
                        )
                        if result.success:
                            st.code(result.stdout or "(no warning events)", language="text")
                        else:
                            st.error(result.stderr or "Failed")


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Certificate Manager
# ══════════════════════════════════════════════════════════════════════════

def page_certificate_manager():
    st.markdown("## Certificate Manager")
    st.markdown("View cluster certificate expiration dates, TLS secrets, and plan renewals.")

    profile = _get_active_profile()
    if not profile:
        return

    _show_profile_summary(profile)

    tab_certs, tab_tls, tab_renew = st.tabs([
        "Cluster Certificates",
        "TLS Secrets",
        "Renewal Guide",
    ])

    # ── Cluster Certificates (kubeadm) ────────────────────────────────────
    with tab_certs:
        st.markdown("### Cluster Certificates (kubeadm)")

        if profile.cluster_source == "imported":
            st.info(
                "Certificate inspection via `kubeadm certs check-expiration` requires SSH access "
                "to control-plane nodes. For imported clusters, use the **TLS Secrets** tab to view "
                "TLS certificates stored in the cluster."
            )
            # Still try to get API server cert info
            if st.button("Check API Server Certificate", key="api_cert_check"):
                with st.spinner("Checking API server certificate..."):
                    cmd = (
                        "get --raw /healthz -v=6 2>&1 || true"
                    )
                    result = run_kubectl(profile, "version --short", timeout=10)
                    if result.success:
                        st.success("API server is reachable and serving valid TLS.")
                        st.code(result.stdout, language="text")
                    else:
                        if "certificate" in (result.stderr or "").lower():
                            st.error("Certificate issue detected:")
                            st.code(result.stderr, language="text")
                        else:
                            st.warning(f"Could not check: {result.stderr}")
        else:
            cp_nodes = profile.get_control_plane_nodes()
            if not cp_nodes:
                st.warning("No control-plane nodes defined.")
            else:
                st.markdown(
                    "Runs `kubeadm certs check-expiration` on control-plane nodes via SSH "
                    "to show certificate validity and expiration dates."
                )
                if st.button("Check Certificate Expiration", type="primary", key="check_certs"):
                    for node in cp_nodes:
                        node_label = f"{node.get('hostname', node.get('ip_address', '?'))} ({node.get('ip_address', '')})"
                        with st.expander(f"Node: {node_label}", expanded=True):
                            with st.spinner(f"Checking certificates on {node_label}..."):
                                result = run_ssh_command(
                                    ip_address=node["ip_address"],
                                    command="sudo kubeadm certs check-expiration 2>/dev/null || echo 'kubeadm certs command not available'",
                                    ssh_user=node.get("ssh_user", "root"),
                                    ssh_port=node.get("ssh_port", 22),
                                    ssh_key_path=node.get("ssh_key_path", "~/.ssh/id_rsa"),
                                    timeout=30,
                                )
                                if result.success and result.stdout.strip():
                                    st.code(result.stdout, language="text")
                                    # Parse for expiring soon
                                    if "RESIDUAL TIME" in result.stdout:
                                        for line in result.stdout.split("\n"):
                                            if any(warn in line.lower() for warn in ["invalid", "expired"]):
                                                st.error(f"Certificate issue: {line.strip()}")
                                else:
                                    st.error(f"Failed: {result.stderr or 'No output'}")

    # ── TLS Secrets ───────────────────────────────────────────────────────
    with tab_tls:
        st.markdown("### TLS Secrets")
        st.markdown("View Kubernetes TLS secrets and their certificate details.")

        if st.button("Load TLS Secrets", type="primary", key="load_tls"):
            cmd = "get secrets -A -o json"
            with st.spinner("Fetching secrets..."):
                result = run_kubectl(profile, cmd, timeout=20)
            if result.success and result.stdout.strip():
                try:
                    import pandas as pd
                    secrets_json = json.loads(result.stdout)
                    tls_secrets = []
                    for secret in secrets_json.get("items", []):
                        if secret.get("type") == "kubernetes.io/tls":
                            meta = secret.get("metadata", {})
                            annotations = meta.get("annotations", {})
                            tls_secrets.append({
                                "Namespace": meta.get("namespace", "?"),
                                "Name": meta.get("name", "?"),
                                "Type": "kubernetes.io/tls",
                                "Created": meta.get("creationTimestamp", "N/A"),
                                "Issuer": annotations.get("cert-manager.io/issuer-name", annotations.get("cert-manager.io/cluster-issuer", "N/A")),
                                "Has cert": "tls.crt" in secret.get("data", {}),
                                "Has key": "tls.key" in secret.get("data", {}),
                            })
                    if tls_secrets:
                        st.markdown(f"**Found {len(tls_secrets)} TLS secret(s)**")
                        st.dataframe(pd.DataFrame(tls_secrets), use_container_width=True, hide_index=True)
                    else:
                        st.info("No TLS secrets found in the cluster.")
                except (json.JSONDecodeError, KeyError) as e:
                    st.error(f"Failed to parse secrets: {e}")
            elif result.success:
                st.info("No secrets found.")
            else:
                st.error("Failed to fetch secrets")
                st.code(result.stderr, language="text")

        # cert-manager status
        st.markdown("---")
        st.markdown("#### cert-manager Status")
        if st.button("Check cert-manager", key="check_certmanager"):
            with st.spinner("Checking cert-manager..."):
                result = run_kubectl(profile, "get pods -n cert-manager --no-headers", timeout=10)
                if result.success and result.stdout.strip():
                    st.success("cert-manager is installed:")
                    st.code(result.stdout, language="text")
                    # Check certificates
                    cert_result = run_kubectl(profile, "get certificates -A --no-headers", timeout=10)
                    if cert_result.success and cert_result.stdout.strip():
                        st.markdown("**Managed Certificates:**")
                        st.code(cert_result.stdout, language="text")
                elif result.success:
                    st.info("cert-manager namespace exists but no pods found.")
                else:
                    st.info("cert-manager does not appear to be installed.")

    # ── Renewal Guide ─────────────────────────────────────────────────────
    with tab_renew:
        st.markdown("### Certificate Renewal Guide")

        st.markdown("""
#### Automatic Renewal (kubeadm)

kubeadm automatically renews certificates during `kubeadm upgrade`. For manual renewal:

```bash
# Renew all certificates
sudo kubeadm certs renew all

# Renew specific certificate
sudo kubeadm certs renew apiserver
sudo kubeadm certs renew apiserver-kubelet-client
sudo kubeadm certs renew front-proxy-client
sudo kubeadm certs renew etcd-server
sudo kubeadm certs renew etcd-peer
sudo kubeadm certs renew etcd-healthcheck-client

# After renewal, restart control plane components
sudo systemctl restart kubelet
```

#### Certificate Authority (CA) Rotation

CA rotation is more complex and requires:
1. Generate new CA certificate and key
2. Distribute to all nodes
3. Re-sign all component certificates
4. Rolling restart of all components

#### cert-manager Renewal

If using cert-manager, certificates are automatically renewed before expiration.
Check cert-manager logs for renewal status:

```bash
kubectl logs -n cert-manager deploy/cert-manager -f
```

#### Best Practices
- Monitor certificate expiration dates regularly
- Set up alerts for certificates expiring within 30 days
- Keep kubeadm version aligned with cluster version for smooth renewals
- Back up `/etc/kubernetes/pki/` before any certificate operations
- Test renewal in a staging environment first
        """)


# ══════════════════════════════════════════════════════════════════════════
#  PAGE: Cost Optimizer
# ══════════════════════════════════════════════════════════════════════════

def page_cost_optimizer():
    st.markdown("## Cost Estimator / Resource Optimizer")
    st.markdown("Analyze resource usage vs requests/limits and identify optimization opportunities.")

    profile = _get_active_profile()
    if not profile:
        return

    _show_profile_summary(profile)

    tab_usage, tab_right_size, tab_idle = st.tabs([
        "Resource Usage",
        "Right-Sizing",
        "Idle Resources",
    ])

    # ── Resource Usage ────────────────────────────────────────────────────
    with tab_usage:
        st.markdown("### Actual Resource Usage vs Requests")
        st.markdown("Compare real CPU/memory usage (from metrics-server) against configured requests and limits.")

        usage_sub = st.radio("View", ["Node Usage", "Pod Usage"], horizontal=True, key="usage_view")

        if usage_sub == "Node Usage":
            if st.button("Load Node Usage", type="primary", key="load_node_usage"):
                with st.spinner("Fetching node metrics..."):
                    result = run_kubectl(profile, "top nodes --no-headers", timeout=15)
                if result.success and result.stdout.strip():
                    import pandas as pd
                    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                    node_usage = []
                    for line in lines:
                        parts = line.split()
                        if len(parts) >= 5:
                            node_usage.append({
                                "Node": parts[0],
                                "CPU (cores)": parts[1],
                                "CPU %": parts[2],
                                "Memory": parts[3],
                                "Memory %": parts[4],
                            })
                    if node_usage:
                        st.dataframe(pd.DataFrame(node_usage), use_container_width=True, hide_index=True)
                        # Chart
                        try:
                            import plotly.graph_objects as go
                            fig = go.Figure()
                            names = [n["Node"] for n in node_usage]
                            cpu_pcts = [int(n["CPU %"].replace("%", "")) for n in node_usage]
                            mem_pcts = [int(n["Memory %"].replace("%", "")) for n in node_usage]
                            fig.add_trace(go.Bar(name="CPU %", x=names, y=cpu_pcts, marker_color="#326CE5"))
                            fig.add_trace(go.Bar(name="Memory %", x=names, y=mem_pcts, marker_color="#764ba2"))
                            fig.update_layout(
                                title="Node Resource Utilization",
                                yaxis_title="Utilization %",
                                barmode="group",
                                height=400,
                            )
                            fig.add_hline(y=80, line_dash="dash", line_color="red", annotation_text="80% threshold")
                            st.plotly_chart(fig, use_container_width=True)
                        except ImportError:
                            pass
                    else:
                        st.code(result.stdout, language="text")
                elif result.success:
                    st.info("No node metrics available. Is metrics-server installed?")
                else:
                    st.error("Failed to fetch node metrics. Ensure metrics-server is installed.")
                    st.code(result.stderr, language="text")
                    st.info("Install metrics-server via **Monitoring Setup** > **Metrics Components**.")

        elif usage_sub == "Pod Usage":
            pcol1, pcol2 = st.columns([2, 1])
            with pcol1:
                _co_namespaces: list[str] = []
                if profile.cluster_source == "imported" and profile.kubeconfig_content:
                    _co_namespaces = fetch_namespaces(profile.kubeconfig_content)
                if _co_namespaces:
                    pod_usage_ns = st.selectbox("Namespace", ["All Namespaces"] + _co_namespaces, key="pod_usage_ns")
                else:
                    pod_usage_ns = st.text_input("Namespace (blank = all)", value="", key="pod_usage_ns_text")
                    if not pod_usage_ns:
                        pod_usage_ns = "All Namespaces"

            if st.button("Load Pod Usage", type="primary", key="load_pod_usage"):
                ns_flag = "-A" if pod_usage_ns == "All Namespaces" else f"-n {pod_usage_ns}"
                with st.spinner("Fetching pod metrics..."):
                    result = run_kubectl(profile, f"top pods {ns_flag} --no-headers", timeout=20)
                if result.success and result.stdout.strip():
                    import pandas as pd
                    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                    pod_usage = []
                    for line in lines:
                        parts = line.split()
                        if pod_usage_ns == "All Namespaces" and len(parts) >= 4:
                            pod_usage.append({
                                "Namespace": parts[0],
                                "Pod": parts[1],
                                "CPU": parts[2],
                                "Memory": parts[3],
                            })
                        elif len(parts) >= 3:
                            pod_usage.append({
                                "Pod": parts[0],
                                "CPU": parts[1],
                                "Memory": parts[2],
                            })
                    if pod_usage:
                        df = pd.DataFrame(pod_usage)
                        st.dataframe(df, use_container_width=True, hide_index=True)
                        st.markdown(f"**Total pods:** {len(df)}")
                elif result.success:
                    st.info("No pod metrics available.")
                else:
                    st.error("Failed to fetch pod metrics.")
                    st.code(result.stderr, language="text")

    # ── Right-Sizing ──────────────────────────────────────────────────────
    with tab_right_size:
        st.markdown("### Right-Sizing Recommendations")
        st.markdown(
            "Compare actual pod usage against configured requests/limits to find "
            "over-provisioned or under-provisioned workloads."
        )

        rs_col1, rs_col2 = st.columns([2, 1])
        with rs_col1:
            _rs_namespaces: list[str] = []
            if profile.cluster_source == "imported" and profile.kubeconfig_content:
                _rs_namespaces = fetch_namespaces(profile.kubeconfig_content)
            if _rs_namespaces:
                rs_ns = st.selectbox("Namespace", _rs_namespaces, key="rs_ns")
            else:
                rs_ns = st.text_input("Namespace", value="default", key="rs_ns_text")

        if st.button("Analyze Right-Sizing", type="primary", key="analyze_rs"):
            if not rs_ns:
                st.warning("Please specify a namespace.")
            else:
                with st.spinner("Fetching usage and resource specs..."):
                    # Get actual usage
                    usage_result = run_kubectl(
                        profile,
                        f"top pods -n {rs_ns} --no-headers --containers",
                        timeout=20,
                    )
                    # Get resource specs
                    spec_result = run_kubectl(
                        profile,
                        f"get pods -n {rs_ns} -o json",
                        timeout=20,
                    )

                if usage_result.success and spec_result.success:
                    try:
                        import pandas as pd
                        # Parse usage: POD CONTAINER CPU MEM
                        usage_map = {}
                        for line in (usage_result.stdout or "").strip().split("\n"):
                            parts = line.split()
                            if len(parts) >= 4:
                                key = f"{parts[0]}/{parts[1]}"
                                usage_map[key] = {"cpu_usage": parts[2], "mem_usage": parts[3]}

                        # Parse specs
                        pods_json = json.loads(spec_result.stdout)
                        rows = []
                        for pod in pods_json.get("items", []):
                            pod_name = pod.get("metadata", {}).get("name", "?")
                            for container in pod.get("spec", {}).get("containers", []):
                                c_name = container.get("name", "?")
                                res = container.get("resources", {})
                                req_cpu = res.get("requests", {}).get("cpu", "none")
                                req_mem = res.get("requests", {}).get("memory", "none")
                                lim_cpu = res.get("limits", {}).get("cpu", "none")
                                lim_mem = res.get("limits", {}).get("memory", "none")
                                key = f"{pod_name}/{c_name}"
                                usage = usage_map.get(key, {})
                                rows.append({
                                    "Pod": pod_name,
                                    "Container": c_name,
                                    "CPU Usage": usage.get("cpu_usage", "N/A"),
                                    "CPU Request": req_cpu,
                                    "CPU Limit": lim_cpu,
                                    "Mem Usage": usage.get("mem_usage", "N/A"),
                                    "Mem Request": req_mem,
                                    "Mem Limit": lim_mem,
                                })
                        if rows:
                            df = pd.DataFrame(rows)
                            st.dataframe(df, use_container_width=True, hide_index=True)

                            # Recommendations
                            no_req_cpu = sum(1 for r in rows if r["CPU Request"] == "none")
                            no_req_mem = sum(1 for r in rows if r["Mem Request"] == "none")
                            no_lim_cpu = sum(1 for r in rows if r["CPU Limit"] == "none")
                            no_lim_mem = sum(1 for r in rows if r["Mem Limit"] == "none")

                            st.markdown("---")
                            st.markdown("#### Recommendations")
                            if no_req_cpu > 0:
                                st.warning(f"{no_req_cpu} container(s) have **no CPU request** — scheduler cannot make optimal placement decisions.")
                            if no_req_mem > 0:
                                st.warning(f"{no_req_mem} container(s) have **no memory request** — pods may be evicted under pressure.")
                            if no_lim_cpu > 0:
                                st.info(f"{no_lim_cpu} container(s) have **no CPU limit** — they can consume all available CPU on the node.")
                            if no_lim_mem > 0:
                                st.warning(f"{no_lim_mem} container(s) have **no memory limit** — they may be OOMKilled or cause node instability.")
                            if no_req_cpu == 0 and no_req_mem == 0 and no_lim_cpu == 0 and no_lim_mem == 0:
                                st.success("All containers have CPU and memory requests and limits set.")
                        else:
                            st.info("No containers found in this namespace.")
                    except (json.JSONDecodeError, KeyError) as e:
                        st.error(f"Failed to parse data: {e}")
                else:
                    if not usage_result.success:
                        st.error("Failed to fetch pod usage metrics. Is metrics-server installed?")
                        st.code(usage_result.stderr, language="text")
                    if not spec_result.success:
                        st.error("Failed to fetch pod specs.")
                        st.code(spec_result.stderr, language="text")

    # ── Idle Resources ────────────────────────────────────────────────────
    with tab_idle:
        st.markdown("### Idle / Unused Resources")
        st.markdown("Find resources that may be wasting cluster capacity.")

        idle_checks = st.multiselect(
            "Check for",
            [
                "Completed/Failed Jobs",
                "Deployments scaled to 0",
                "Orphaned ConfigMaps",
                "Unbound PVCs",
                "Empty Namespaces",
            ],
            default=["Completed/Failed Jobs", "Deployments scaled to 0", "Unbound PVCs"],
            key="idle_checks",
        )

        if st.button("Scan for Idle Resources", type="primary", key="scan_idle"):
            findings = []

            if "Completed/Failed Jobs" in idle_checks:
                with st.spinner("Checking completed/failed jobs..."):
                    result = run_kubectl(profile, "get jobs -A -o json", timeout=15)
                if result.success and result.stdout.strip():
                    try:
                        jobs = json.loads(result.stdout).get("items", [])
                        old_jobs = []
                        for job in jobs:
                            status = job.get("status", {})
                            conditions = status.get("conditions", [])
                            for cond in conditions:
                                if cond.get("type") in ("Complete", "Failed") and cond.get("status") == "True":
                                    meta = job.get("metadata", {})
                                    old_jobs.append(f"  - {meta.get('namespace', '?')}/{meta.get('name', '?')} ({cond['type']})")
                        if old_jobs:
                            findings.append(("warning", f"**{len(old_jobs)} completed/failed job(s)** can be cleaned up:\n" + "\n".join(old_jobs[:20])))
                        else:
                            findings.append(("success", "No completed/failed jobs found."))
                    except (json.JSONDecodeError, KeyError):
                        findings.append(("error", "Failed to parse jobs data."))

            if "Deployments scaled to 0" in idle_checks:
                with st.spinner("Checking zero-replica deployments..."):
                    result = run_kubectl(profile, "get deployments -A -o json", timeout=15)
                if result.success and result.stdout.strip():
                    try:
                        deploys = json.loads(result.stdout).get("items", [])
                        zero_deploys = []
                        for dep in deploys:
                            replicas = dep.get("spec", {}).get("replicas", 1)
                            if replicas == 0:
                                meta = dep.get("metadata", {})
                                zero_deploys.append(f"  - {meta.get('namespace', '?')}/{meta.get('name', '?')}")
                        if zero_deploys:
                            findings.append(("warning", f"**{len(zero_deploys)} deployment(s) scaled to 0 replicas:**\n" + "\n".join(zero_deploys[:20])))
                        else:
                            findings.append(("success", "No zero-replica deployments found."))
                    except (json.JSONDecodeError, KeyError):
                        findings.append(("error", "Failed to parse deployment data."))

            if "Unbound PVCs" in idle_checks:
                with st.spinner("Checking unbound PVCs..."):
                    result = run_kubectl(profile, "get pvc -A --no-headers", timeout=15)
                if result.success and result.stdout.strip():
                    lines = [l for l in result.stdout.strip().split("\n") if l.strip()]
                    pending_pvcs = [l for l in lines if "Pending" in l]
                    if pending_pvcs:
                        findings.append(("warning", f"**{len(pending_pvcs)} PVC(s) in Pending state** (not bound to a PV):\n```\n" + "\n".join(pending_pvcs[:10]) + "\n```"))
                    else:
                        findings.append(("success", "All PVCs are bound."))
                elif result.success:
                    findings.append(("info", "No PVCs found."))

            if "Empty Namespaces" in idle_checks:
                with st.spinner("Checking empty namespaces..."):
                    ns_result = run_kubectl(profile, "get namespaces --no-headers", timeout=10)
                if ns_result.success and ns_result.stdout.strip():
                    ns_lines = [l.split()[0] for l in ns_result.stdout.strip().split("\n") if l.strip()]
                    system_ns = {"kube-system", "kube-public", "kube-node-lease", "default"}
                    empty_ns = []
                    for ns in ns_lines:
                        if ns in system_ns:
                            continue
                        pod_r = run_kubectl(profile, f"get pods -n {ns} --no-headers", timeout=10)
                        if pod_r.success and not pod_r.stdout.strip():
                            empty_ns.append(ns)
                    if empty_ns:
                        findings.append(("info", f"**{len(empty_ns)} namespace(s) with no pods:**\n  - " + "\n  - ".join(empty_ns[:15])))
                    else:
                        findings.append(("success", "No empty non-system namespaces found."))

            if "Orphaned ConfigMaps" in idle_checks:
                findings.append(("info", "Orphaned ConfigMap detection requires cross-referencing all pod specs — use the **Resource Viewer** to manually inspect ConfigMaps per namespace."))

            # Display findings
            st.markdown("---")
            st.markdown("#### Findings")
            for level, msg in findings:
                if level == "warning":
                    st.warning(msg)
                elif level == "error":
                    st.error(msg)
                elif level == "success":
                    st.success(msg)
                else:
                    st.info(msg)


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
    if profile.cluster_source == "imported":
        cols = st.columns(4)
        cols[0].metric("Profile", profile.name)
        cols[1].metric("K8s Version", profile.kubernetes_version)
        cols[2].metric("Source", "Imported (kubeconfig)")
        cols[3].metric("Status", profile.status.upper())
        with st.expander("Cluster Details", expanded=False):
            st.markdown(f"**Description:** {profile.description or 'N/A'}")
            st.markdown(f"**Kubeconfig:** {'Loaded' if profile.kubeconfig_content else 'Not loaded'}")

            # Fetch live cluster info from kubeconfig
            if profile.kubeconfig_content:
                node_result = run_kubectl(
                    profile,
                    "get nodes -o wide --no-headers",
                    timeout=10,
                )
                if node_result.success and node_result.stdout.strip():
                    st.markdown("---")
                    st.markdown("**Cluster Nodes:**")
                    node_lines = [l for l in node_result.stdout.strip().split("\n") if l.strip()]
                    node_data = []
                    for line in node_lines:
                        parts = line.split()
                        if len(parts) >= 5:
                            node_data.append({
                                "Name": parts[0],
                                "Status": parts[1],
                                "Roles": parts[2] if parts[2] != "<none>" else "worker",
                                "Age": parts[3],
                                "Kubelet Version": parts[4],
                                "Internal IP": parts[5] if len(parts) > 5 else "N/A",
                                "OS Image": " ".join(parts[7:9]) if len(parts) > 8 else (parts[7] if len(parts) > 7 else "N/A"),
                                "Container Runtime": parts[-1] if len(parts) > 9 else "N/A",
                            })
                    if node_data:
                        import pandas as pd
                        st.dataframe(
                            pd.DataFrame(node_data),
                            use_container_width=True,
                            hide_index=True,
                        )
                        # Summary
                        cp_count = sum(1 for n in node_data if "control-plane" in n["Roles"] or "master" in n["Roles"])
                        worker_count = len(node_data) - cp_count
                        ready_count = sum(1 for n in node_data if "Ready" in n["Status"])
                        st.markdown(
                            f"**Total:** {len(node_data)} node(s) — "
                            f"{cp_count} control-plane, {worker_count} worker | "
                            f"**Ready:** {ready_count}/{len(node_data)}"
                        )
                    else:
                        st.code(node_result.stdout, language="text")

                    # Cluster info (API server endpoint)
                    info_result = run_kubectl(profile, "cluster-info", timeout=10)
                    if info_result.success and info_result.stdout.strip():
                        st.markdown("---")
                        st.markdown("**Cluster Info:**")
                        # Strip ANSI color codes for clean display
                        import re
                        clean_info = re.sub(r'\x1b\[[0-9;]*m', '', info_result.stdout)
                        st.code(clean_info.strip(), language="text")
                elif node_result.success:
                    st.info("Connected to cluster but no nodes found.")
                else:
                    st.warning(
                        f"Could not fetch cluster details: {node_result.stderr or 'kubectl command failed'}. "
                        "Verify that kubectl is installed and the kubeconfig is valid."
                    )
    else:
        cols = st.columns(5)
        cols[0].metric("Profile", profile.name)
        cols[1].metric("K8s Version", profile.kubernetes_version)
        cols[2].metric("Runtime", f"CRI-O {profile.crio_version}")
        cols[3].metric("CNI", "Flannel")
        cols[4].metric("Nodes", f"{len(profile.get_control_plane_nodes())} CP + {len(profile.get_worker_nodes())} W")

        with st.expander("Storage & Proxy Details", expanded=False):
            scol1, scol2, scol3 = st.columns(3)
            with scol1:
                st.markdown(f"**CRI-O Root:** `{profile.crio_root}`")
                st.markdown(f"**CRI-O RunRoot:** `{profile.crio_runroot}`")
            with scol2:
                st.markdown(f"**Kubelet Dir:** `{profile.kubelet_root}`")
                st.markdown(f"**Log Root:** `{profile.log_root}`")
            with scol3:
                if profile.http_proxy or profile.https_proxy:
                    st.markdown(f"**HTTP Proxy:** `{profile.http_proxy or 'N/A'}`")
                    st.markdown(f"**HTTPS Proxy:** `{profile.https_proxy or 'N/A'}`")
                    if profile.no_proxy:
                        st.markdown(f"**No Proxy:** `{profile.no_proxy}`")
                if profile.http_proxy_alt or profile.https_proxy_alt:
                    st.markdown(f"**Alt HTTP Proxy:** `{profile.http_proxy_alt or 'N/A'}`")
                    st.markdown(f"**Alt HTTPS Proxy:** `{profile.https_proxy_alt or 'N/A'}`")
                if not (profile.http_proxy or profile.https_proxy or profile.http_proxy_alt or profile.https_proxy_alt):
                    st.markdown("**Proxy:** Not configured")


# ── Main Router ───────────────────────────────────────────────────────────

def main():
    page = render_sidebar()

    if page == "Multi-Cluster Dashboard":
        page_multi_cluster_dashboard()
    elif page == "Profile Manager":
        page_profile_manager()
    elif page == "Cluster Creation":
        page_cluster_creation()
    elif page == "Resource Viewer":
        page_resource_viewer()
    elif page == "Cluster Debugger":
        page_cluster_debugger()
    elif page == "Monitoring Setup":
        page_monitoring_setup()
    elif page == "Log Analysis":
        page_log_analysis()
    elif page == "Upgrade Planner":
        page_upgrade_planner()
    elif page == "Certificate Manager":
        page_certificate_manager()
    elif page == "Cost Optimizer":
        page_cost_optimizer()
    elif page == "AI Assistant":
        page_ai_assistant()


if __name__ == "__main__":
    main()
