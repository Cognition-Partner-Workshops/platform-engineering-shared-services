"""Cluster Debugger — Diagnose K8s issues and provide LLM-powered recommendations."""

from modules.cluster_creator import run_ssh_command, SSHResult
from modules.profile_manager import ClusterProfile


# ── Diagnostic command definitions ────────────────────────────────────────

DIAGNOSTIC_COMMANDS = {
    "Node Status": "kubectl get nodes -o wide",
    "Pod Status (All Namespaces)": "kubectl get pods -A -o wide",
    "Events (Recent)": "kubectl get events -A --sort-by='.lastTimestamp' | tail -50",
    "Component Status": "kubectl get componentstatuses 2>/dev/null; kubectl get --raw='/healthz?verbose' 2>/dev/null || true",
    "System Pods": "kubectl -n kube-system get pods -o wide",
    "Node Resources": "kubectl top nodes 2>/dev/null || echo 'metrics-server not installed'",
    "Pod Resources": "kubectl top pods -A 2>/dev/null || echo 'metrics-server not installed'",
    "Cluster Info": "kubectl cluster-info",
    "CRI-O Status": "systemctl status crio --no-pager -l",
    "Kubelet Status": "systemctl status kubelet --no-pager -l",
    "Kubelet Logs (Recent)": "journalctl -u kubelet --no-pager -n 50",
    "CRI-O Logs (Recent)": "journalctl -u crio --no-pager -n 50",
    "Flannel Status": "kubectl -n kube-flannel get pods -o wide 2>/dev/null || kubectl -n kube-system get pods -l app=flannel -o wide 2>/dev/null || echo 'Flannel pods not found'",
    "Network Policies": "kubectl get networkpolicies -A",
    "Services": "kubectl get svc -A",
    "PVCs": "kubectl get pvc -A",
    "Ingresses": "kubectl get ingress -A 2>/dev/null || true",
    "Disk Usage": "df -h / /var/lib/containers /var/lib/kubelet 2>/dev/null || df -h /",
    "Memory Info": "free -h",
    "DNS Resolution": "kubectl run dns-test --image=busybox:1.36 --rm -it --restart=Never -- nslookup kubernetes.default 2>/dev/null || echo 'DNS test skipped'",
    "Certificate Expiry": "kubeadm certs check-expiration 2>/dev/null || echo 'Not a kubeadm node or kubeadm not found'",
}

CATEGORY_MAP = {
    "Cluster Overview": [
        "Node Status",
        "Pod Status (All Namespaces)",
        "Cluster Info",
        "Component Status",
    ],
    "Pod & Workload Health": [
        "Pod Status (All Namespaces)",
        "System Pods",
        "Events (Recent)",
    ],
    "Resource Usage": [
        "Node Resources",
        "Pod Resources",
        "Disk Usage",
        "Memory Info",
    ],
    "Networking": [
        "Flannel Status",
        "Network Policies",
        "Services",
        "Ingresses",
        "DNS Resolution",
    ],
    "Container Runtime & Kubelet": [
        "CRI-O Status",
        "Kubelet Status",
        "CRI-O Logs (Recent)",
        "Kubelet Logs (Recent)",
    ],
    "Security & Certificates": [
        "Certificate Expiry",
        "Network Policies",
    ],
    "Storage": [
        "PVCs",
        "Disk Usage",
    ],
}


def run_diagnostic(
    control_plane_node: dict,
    command_name: str,
) -> SSHResult:
    """Run a single diagnostic command on the control-plane node."""
    command = DIAGNOSTIC_COMMANDS.get(command_name)
    if not command:
        return SSHResult(
            hostname=control_plane_node["ip_address"],
            command=command_name,
            return_code=1,
            stdout="",
            stderr=f"Unknown diagnostic command: {command_name}",
            success=False,
        )
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=command,
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=60,
    )


def run_category_diagnostics(
    control_plane_node: dict,
    category: str,
) -> dict[str, SSHResult]:
    """Run all diagnostic commands for a given category."""
    results = {}
    command_names = CATEGORY_MAP.get(category, [])
    for name in command_names:
        results[name] = run_diagnostic(control_plane_node, name)
    return results


def run_all_diagnostics(control_plane_node: dict) -> dict[str, SSHResult]:
    """Run every diagnostic command."""
    results = {}
    for name in DIAGNOSTIC_COMMANDS:
        results[name] = run_diagnostic(control_plane_node, name)
    return results


def run_custom_command(
    control_plane_node: dict,
    command: str,
) -> SSHResult:
    """Run a custom command on the control-plane node."""
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=command,
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=60,
    )


def format_diagnostics_for_llm(results: dict[str, SSHResult]) -> str:
    """Format diagnostic results into a text block for the LLM."""
    sections = []
    for name, result in results.items():
        status = "OK" if result.success else "FAILED"
        output = result.stdout if result.success else result.stderr
        sections.append(
            f"### {name} [{status}]\n"
            f"```\n{output.strip()}\n```\n"
        )
    return "\n".join(sections)


def analyze_diagnostics(
    results: dict[str, SSHResult],
    user_description: str = "",
    profile: ClusterProfile | None = None,
) -> str:
    """Send diagnostic results to the LLM for analysis and recommendations.

    Returns a graceful message when the LLM is not configured.
    """
    from modules.llm_client import query_llm  # lazy import — LLM is optional

    diag_text = format_diagnostics_for_llm(results)

    cluster_info = ""
    if profile:
        cluster_info = f"""
Cluster Configuration:
- Kubernetes: {profile.kubernetes_version}
- Runtime: CRI-O {profile.crio_version}
- CNI: Flannel
- Pod CIDR: {profile.pod_cidr}
- Service CIDR: {profile.service_cidr}
"""

    prompt = f"""Analyze the following Kubernetes cluster diagnostic output and provide a detailed assessment.
{cluster_info}

User's issue description: {user_description or 'General health check'}

== Diagnostic Output ==
{diag_text}
== End Diagnostic Output ==

Please provide:
1. **Health Summary**: Overall cluster health status (Healthy / Degraded / Critical)
2. **Issues Found**: List each issue with severity (Critical / Warning / Info)
3. **Root Cause Analysis**: For each issue, explain the likely root cause
4. **Remediation Steps**: Specific commands or actions to fix each issue
5. **Preventive Recommendations**: Steps to prevent these issues in the future

Format your response with clear headings and actionable commands where applicable.
"""
    return query_llm(prompt)


def get_debug_suggestion(
    error_message: str,
    context: str = "",
) -> str:
    """Get a quick debugging suggestion from the LLM for a specific error.

    Returns a graceful message when the LLM is not configured.
    """
    from modules.llm_client import query_llm  # lazy import — LLM is optional

    prompt = f"""I encountered the following error in my Kubernetes cluster (CRI-O + Flannel):

Error: {error_message}

Additional context: {context or 'None'}

Provide a concise diagnosis and the exact commands to fix this issue.
"""
    return query_llm(prompt)


def check_pod_issues(control_plane_node: dict, namespace: str = "") -> SSHResult:
    """Check for pods in non-running states."""
    ns_flag = f"-n {namespace}" if namespace else "-A"
    command = (
        f"kubectl get pods {ns_flag} --field-selector="
        "'status.phase!=Running,status.phase!=Succeeded' -o wide 2>/dev/null; "
        f"echo '---DESCRIBE---'; "
        f"for pod in $(kubectl get pods {ns_flag} --field-selector="
        "'status.phase!=Running,status.phase!=Succeeded' "
        "-o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name} {end}' 2>/dev/null); do "
        "ns=$(echo $pod | cut -d/ -f1); "
        "name=$(echo $pod | cut -d/ -f2); "
        "echo \"=== $ns/$name ===\"; "
        "kubectl describe pod $name -n $ns 2>/dev/null | tail -20; "
        "done"
    )
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=command,
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=60,
    )
