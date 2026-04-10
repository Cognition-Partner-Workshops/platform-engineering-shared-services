"""Cluster Debugger — Diagnose K8s issues and provide LLM-powered recommendations.

Supports both provisioned clusters (SSH-based) and imported clusters (kubeconfig-based).
"""

import os
import subprocess

from modules.cluster_creator import run_ssh_command, SSHResult
from modules.profile_manager import ClusterProfile
import config


# ── Diagnostic command definitions ────────────────────────────────────────

# kubectl-only commands (work for both imported and provisioned clusters)
KUBECTL_DIAGNOSTIC_COMMANDS = {
    "Node Status": "get nodes -o wide",
    "Pod Status (All Namespaces)": "get pods -A -o wide",
    "Events (Recent)": "get events -A --sort-by=.lastTimestamp",
    "Component Status": "get componentstatuses",
    "System Pods": "-n kube-system get pods -o wide",
    "Node Resources": "top nodes",
    "Pod Resources": "top pods -A",
    "Cluster Info": "cluster-info",
    "Flannel Status": "-n kube-flannel get pods -o wide",
    "Network Policies": "get networkpolicies -A",
    "Services": "get svc -A",
    "PVCs": "get pvc -A",
    "Ingresses": "get ingress -A",
    "Disk Usage": "top nodes",
}

# Full SSH commands (backward-compat for provisioned clusters)
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


def _run_local_kubectl(kubeconfig_content: str, kubectl_args: str, timeout: int = 60) -> SSHResult:
    """Run a kubectl command locally using the given kubeconfig content."""
    kubectl = config.get_kubectl_path()
    if not kubectl:
        return SSHResult(
            hostname="local", command="kubectl " + kubectl_args, return_code=1,
            stdout="",
            stderr=(
                "kubectl not found on this machine.\n\n"
                "Install kubectl:\n"
                "  curl -LO https://dl.k8s.io/release/$(curl -Ls https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl\n"
                "  chmod +x kubectl && sudo mv kubectl /usr/local/bin/\n\n"
                "Or on macOS: brew install kubectl\n"
                "Or see: https://kubernetes.io/docs/tasks/tools/"
            ),
            success=False,
        )
    kubeconfig_path = config.get_kubeconfig_path("_debug_temp")
    with open(kubeconfig_path, "w") as f:
        f.write(kubeconfig_content)
    full_cmd = f"{kubectl} --kubeconfig=\"{kubeconfig_path}\" {kubectl_args}"
    try:
        proc = subprocess.run(full_cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return SSHResult(
            hostname="local", command=full_cmd, return_code=proc.returncode,
            stdout=proc.stdout, stderr=proc.stderr, success=proc.returncode == 0,
        )
    except subprocess.TimeoutExpired:
        return SSHResult(
            hostname="local", command=full_cmd, return_code=-1,
            stdout="", stderr=f"Command timed out after {timeout}s", success=False,
        )
    except Exception as e:
        return SSHResult(
            hostname="local", command=full_cmd, return_code=-1,
            stdout="", stderr=str(e), success=False,
        )


def get_available_commands(profile: ClusterProfile) -> dict[str, str]:
    """Return available diagnostic commands based on cluster source."""
    if profile.cluster_source == "imported":
        return dict(KUBECTL_DIAGNOSTIC_COMMANDS)
    return dict(DIAGNOSTIC_COMMANDS)


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
    control_plane_node: dict | None,
    command_name: str,
    profile: ClusterProfile | None = None,
) -> SSHResult:
    """Run a single diagnostic command.

    For imported clusters, uses kubectl locally with kubeconfig.
    For provisioned clusters, uses SSH to the control-plane node.
    """
    # Imported cluster path
    if profile and profile.cluster_source == "imported" and profile.kubeconfig_content:
        kubectl_args = KUBECTL_DIAGNOSTIC_COMMANDS.get(command_name)
        if kubectl_args is None:
            return SSHResult(
                hostname="local", command=command_name, return_code=1,
                stdout="",
                stderr=f"Command '{command_name}' requires SSH (not available for imported clusters).",
                success=False,
            )
        return _run_local_kubectl(profile.kubeconfig_content, kubectl_args, timeout=60)

    # Provisioned cluster path (SSH)
    if not control_plane_node:
        return SSHResult(
            hostname="unknown", command=command_name, return_code=1,
            stdout="", stderr="No control-plane node available.", success=False,
        )
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
    control_plane_node: dict | None,
    category: str,
    profile: ClusterProfile | None = None,
) -> dict[str, SSHResult]:
    """Run all diagnostic commands for a given category."""
    results = {}
    command_names = CATEGORY_MAP.get(category, [])
    for name in command_names:
        results[name] = run_diagnostic(control_plane_node, name, profile=profile)
    return results


def run_all_diagnostics(
    control_plane_node: dict | None,
    profile: ClusterProfile | None = None,
) -> dict[str, SSHResult]:
    """Run every diagnostic command."""
    commands = get_available_commands(profile) if profile else DIAGNOSTIC_COMMANDS
    results = {}
    for name in commands:
        results[name] = run_diagnostic(control_plane_node, name, profile=profile)
    return results


def run_custom_command(
    control_plane_node: dict | None,
    command: str,
    profile: ClusterProfile | None = None,
) -> SSHResult:
    """Run a custom command. For imported clusters, runs kubectl locally."""
    if profile and profile.cluster_source == "imported" and profile.kubeconfig_content:
        cmd = command.strip()
        if cmd.startswith("kubectl "):
            cmd = cmd[len("kubectl "):]
        return _run_local_kubectl(profile.kubeconfig_content, cmd, timeout=60)

    if not control_plane_node:
        return SSHResult(
            hostname="unknown", command=command, return_code=1,
            stdout="", stderr="No control-plane node available.", success=False,
        )
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


def check_pod_issues(
    control_plane_node: dict | None,
    namespace: str = "",
    profile: ClusterProfile | None = None,
) -> SSHResult:
    """Check for pods in non-running states."""
    ns_flag = f"-n {namespace}" if namespace else "-A"

    if profile and profile.cluster_source == "imported" and profile.kubeconfig_content:
        kubectl_args = (
            f"get pods {ns_flag} "
            "--field-selector=status.phase!=Running,status.phase!=Succeeded -o wide"
        )
        return _run_local_kubectl(profile.kubeconfig_content, kubectl_args, timeout=60)

    if not control_plane_node:
        return SSHResult(
            hostname="unknown", command="check_pod_issues", return_code=1,
            stdout="", stderr="No control-plane node available.", success=False,
        )

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
