"""Log Analyzer — Kubernetes log collection, parsing, error correlation, and analysis.

Supports both provisioned clusters (SSH-based) and imported clusters (kubeconfig-based).
"""

import os
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from modules.cluster_creator import run_ssh_command, SSHResult
from modules.profile_manager import ClusterProfile
import config


def _run_local_shell(kubeconfig_content: str, command: str, timeout: int = 60) -> SSHResult:
    """Run a shell command locally with KUBECONFIG set from profile content."""
    kubeconfig_path = os.path.join(config.DATA_DIR, "kubeconfigs", "_log_temp.kubeconfig")
    os.makedirs(os.path.dirname(kubeconfig_path), exist_ok=True)
    with open(kubeconfig_path, "w") as f:
        f.write(kubeconfig_content)
    env = dict(os.environ, KUBECONFIG=kubeconfig_path)
    try:
        proc = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
        return SSHResult(
            hostname="local", command=command, return_code=proc.returncode,
            stdout=proc.stdout, stderr=proc.stderr, success=proc.returncode == 0,
        )
    except subprocess.TimeoutExpired:
        return SSHResult(
            hostname="local", command=command, return_code=-1,
            stdout="", stderr=f"Command timed out after {timeout}s", success=False,
        )
    except Exception as e:
        return SSHResult(
            hostname="local", command=command, return_code=-1,
            stdout="", stderr=str(e), success=False,
        )


def _run_on_cluster(control_plane_node: dict | None, command: str, profile: ClusterProfile | None = None, timeout: int = 60) -> SSHResult:
    """Route command to local shell or SSH based on cluster source."""
    if profile and profile.cluster_source == "imported" and profile.kubeconfig_content:
        return _run_local_shell(profile.kubeconfig_content, command, timeout=timeout)
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
        timeout=timeout,
    )


@dataclass
class LogEntry:
    """Represents a parsed log line."""

    timestamp: str = ""
    level: str = "INFO"
    source: str = ""
    message: str = ""
    raw: str = ""


@dataclass
class LogAnalysisResult:
    """Results from log analysis."""

    total_lines: int = 0
    error_count: int = 0
    warning_count: int = 0
    error_patterns: dict[str, int] = field(default_factory=dict)
    warning_patterns: dict[str, int] = field(default_factory=dict)
    timeline: list[dict] = field(default_factory=list)
    correlated_errors: list[dict] = field(default_factory=list)


# ── Log collection commands ───────────────────────────────────────────────

# SSH-only sources (journalctl requires node access)
SSH_ONLY_LOG_SOURCES = {"Kubelet", "CRI-O"}

LOG_SOURCES = {
    "Kubelet": "journalctl -u kubelet --no-pager -n {lines} --since '{since}'",
    "CRI-O": "journalctl -u crio --no-pager -n {lines} --since '{since}'",
    "API Server": "kubectl logs -n kube-system -l component=kube-apiserver --tail={lines} --since={since_k8s}",
    "Controller Manager": "kubectl logs -n kube-system -l component=kube-controller-manager --tail={lines} --since={since_k8s}",
    "Scheduler": "kubectl logs -n kube-system -l component=kube-scheduler --tail={lines} --since={since_k8s}",
    "CoreDNS": "kubectl logs -n kube-system -l k8s-app=kube-dns --tail={lines} --since={since_k8s}",
    "Flannel": "kubectl logs -n kube-flannel -l app=flannel --tail={lines} --since={since_k8s} 2>/dev/null || kubectl logs -n kube-system -l app=flannel --tail={lines} --since={since_k8s} 2>/dev/null || echo 'Flannel logs not found'",
    "etcd": "kubectl logs -n kube-system -l component=etcd --tail={lines} --since={since_k8s}",
    "Events": "kubectl get events -A --sort-by='.lastTimestamp' | tail -{lines}",
}

def get_available_log_sources(profile: ClusterProfile | None = None) -> list[str]:
    """Return log sources available for the given cluster type."""
    if profile and profile.cluster_source == "imported":
        return [s for s in LOG_SOURCES if s not in SSH_ONLY_LOG_SOURCES]
    return list(LOG_SOURCES.keys())


POD_LOG_COMMAND = "kubectl logs {pod_ref} --tail={lines} --since={since_k8s} {container_flag}"
POD_PREVIOUS_LOG_COMMAND = "kubectl logs {pod_ref} --previous --tail={lines} {container_flag} 2>/dev/null || echo 'No previous logs available'"


def collect_logs(
    control_plane_node: dict | None,
    source: str,
    lines: int = 200,
    since: str = "1 hour ago",
    since_k8s: str = "1h",
    profile: ClusterProfile | None = None,
) -> SSHResult:
    """Collect logs from a specific source on the cluster."""
    # Block SSH-only sources for imported clusters
    if profile and profile.cluster_source == "imported" and source in SSH_ONLY_LOG_SOURCES:
        return SSHResult(
            hostname="local", command=source, return_code=1,
            stdout="",
            stderr=f"'{source}' logs require SSH access (not available for imported clusters).",
            success=False,
        )

    cmd_template = LOG_SOURCES.get(source)
    if not cmd_template:
        return SSHResult(
            hostname=control_plane_node["ip_address"] if control_plane_node else "local",
            command=source,
            return_code=1,
            stdout="",
            stderr=f"Unknown log source: {source}",
            success=False,
        )

    command = cmd_template.format(
        lines=lines,
        since=since,
        since_k8s=since_k8s,
    )

    return _run_on_cluster(control_plane_node, command, profile=profile, timeout=60)


def collect_pod_logs(
    control_plane_node: dict | None,
    namespace: str,
    pod_name: str,
    container: str = "",
    lines: int = 200,
    since_k8s: str = "1h",
    previous: bool = False,
    profile: ClusterProfile | None = None,
) -> SSHResult:
    """Collect logs from a specific pod."""
    pod_ref = f"-n {namespace} {pod_name}"
    container_flag = f"-c {container}" if container else ""

    if previous:
        command = POD_PREVIOUS_LOG_COMMAND.format(
            pod_ref=pod_ref,
            lines=lines,
            container_flag=container_flag,
        )
    else:
        command = POD_LOG_COMMAND.format(
            pod_ref=pod_ref,
            lines=lines,
            since_k8s=since_k8s,
            container_flag=container_flag,
        )

    return _run_on_cluster(control_plane_node, command, profile=profile, timeout=60)


def collect_multi_source_logs(
    control_plane_node: dict | None,
    sources: list[str],
    lines: int = 100,
    since: str = "1 hour ago",
    since_k8s: str = "1h",
    profile: ClusterProfile | None = None,
) -> dict[str, SSHResult]:
    """Collect logs from multiple sources."""
    results = {}
    for source in sources:
        results[source] = collect_logs(
            control_plane_node, source, lines, since, since_k8s, profile=profile
        )
    return results


# ── Log parsing ───────────────────────────────────────────────────────────

ERROR_PATTERNS = [
    re.compile(r"\b(?:error|err|fatal|panic|fail(?:ed|ure)?)\b", re.IGNORECASE),
]

WARNING_PATTERNS = [
    re.compile(r"\b(?:warn(?:ing)?|deprecated)\b", re.IGNORECASE),
]

TIMESTAMP_PATTERNS = [
    re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})"),
    re.compile(r"([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})"),
]


def parse_log_line(line: str, source: str = "") -> LogEntry:
    """Parse a single log line into a LogEntry."""
    entry = LogEntry(raw=line, source=source)

    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if match:
            entry.timestamp = match.group(1)
            break

    for pattern in ERROR_PATTERNS:
        if pattern.search(line):
            entry.level = "ERROR"
            break
    else:
        for pattern in WARNING_PATTERNS:
            if pattern.search(line):
                entry.level = "WARNING"
                break

    entry.message = line.strip()
    return entry


def analyze_logs(log_text: str, source: str = "") -> LogAnalysisResult:
    """Analyze a block of log text and extract patterns."""
    result = LogAnalysisResult()
    lines = log_text.strip().split("\n")
    result.total_lines = len(lines)

    error_messages = []
    warning_messages = []

    for line in lines:
        if not line.strip():
            continue
        entry = parse_log_line(line, source)

        if entry.level == "ERROR":
            result.error_count += 1
            normalized = _normalize_error(entry.message)
            error_messages.append(normalized)
        elif entry.level == "WARNING":
            result.warning_count += 1
            normalized = _normalize_error(entry.message)
            warning_messages.append(normalized)

    result.error_patterns = dict(Counter(error_messages).most_common(20))
    result.warning_patterns = dict(Counter(warning_messages).most_common(20))

    return result


def _normalize_error(message: str) -> str:
    """Normalize an error message by removing variable parts for grouping."""
    normalized = re.sub(r"\b[0-9a-f]{8,}\b", "<ID>", message)
    normalized = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?", "<IP>", normalized)
    normalized = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*", "<TS>", normalized)
    normalized = re.sub(r"pod/[\w-]+", "pod/<NAME>", normalized)
    normalized = re.sub(r"node/[\w.-]+", "node/<NAME>", normalized)
    if len(normalized) > 150:
        normalized = normalized[:150] + "..."
    return normalized


def correlate_errors(
    multi_source_results: dict[str, SSHResult],
) -> list[dict]:
    """Correlate errors across multiple log sources to find related issues."""
    all_errors = []

    for source, result in multi_source_results.items():
        if not result.success:
            continue
        for line in result.stdout.split("\n"):
            entry = parse_log_line(line, source)
            if entry.level == "ERROR":
                all_errors.append({
                    "source": source,
                    "timestamp": entry.timestamp,
                    "message": entry.message,
                })

    all_errors.sort(key=lambda e: e.get("timestamp", ""))

    correlated = []
    window_seconds = 30
    used = set()

    for i, err in enumerate(all_errors):
        if i in used:
            continue
        group = [err]
        used.add(i)

        for j in range(i + 1, len(all_errors)):
            if j in used:
                continue
            if all_errors[j].get("source") != err.get("source"):
                group.append(all_errors[j])
                used.add(j)

        if len(group) > 1:
            correlated.append({
                "primary": err,
                "related": group[1:],
                "sources_involved": list({e["source"] for e in group}),
            })

    return correlated


# ── LLM-powered analysis ─────────────────────────────────────────────────

def llm_analyze_logs(
    log_text: str,
    source: str = "",
    context: str = "",
) -> str:
    """Send log output to the LLM for deep analysis.

    Returns a graceful message when the LLM is not configured.
    """
    from modules.llm_client import query_llm  # lazy import — LLM is optional

    truncated = log_text[-8000:] if len(log_text) > 8000 else log_text

    prompt = f"""Analyze the following Kubernetes logs and provide a detailed assessment.

Log Source: {source or 'Multiple sources'}
Context: {context or 'General analysis'}

== Log Output ==
{truncated}
== End Log Output ==

Please provide:
1. **Error Summary**: List all distinct errors found with frequency
2. **Root Cause Analysis**: For each error pattern, explain the likely root cause
3. **Error Correlation**: Identify errors that are likely related / cascading
4. **Impact Assessment**: What is the impact of these errors on the cluster?
5. **Remediation Steps**: Specific commands to fix each issue
6. **Patterns & Trends**: Any concerning patterns (increasing errors, recurring issues)
"""
    return query_llm(prompt)


def llm_correlate_analysis(
    multi_source_logs: dict[str, str],
    issue_description: str = "",
) -> str:
    """Send logs from multiple sources to the LLM for cross-source correlation.

    Returns a graceful message when the LLM is not configured.
    """
    from modules.llm_client import query_llm  # lazy import — LLM is optional

    log_sections = []
    for source, log_text in multi_source_logs.items():
        truncated = log_text[-3000:] if len(log_text) > 3000 else log_text
        log_sections.append(f"### {source}\n```\n{truncated}\n```\n")

    all_logs = "\n".join(log_sections)

    prompt = f"""Perform a cross-source correlation analysis on these Kubernetes cluster logs.

Issue Description: {issue_description or 'General health analysis'}

== Multi-Source Logs ==
{all_logs}
== End Logs ==

Please provide:
1. **Cross-Source Correlation**: Identify errors that appear related across different components
2. **Causal Chain**: Determine the sequence of events / root cause chain
3. **Timeline Reconstruction**: Reconstruct what happened based on timestamps
4. **Root Cause**: Identify the single most likely root cause
5. **Remediation Plan**: Step-by-step plan to resolve the issue
6. **Monitoring Recommendations**: What alerts/metrics should be added to catch this earlier
"""
    return query_llm(prompt)


def get_pod_list(
    control_plane_node: dict | None,
    namespace: str = "",
    profile: ClusterProfile | None = None,
) -> SSHResult:
    """Get list of pods for the log analysis UI."""
    ns_flag = f"-n {namespace}" if namespace else "-A"
    command = f"kubectl get pods {ns_flag} -o custom-columns='NAMESPACE:.metadata.namespace,NAME:.metadata.name,STATUS:.status.phase,CONTAINERS:.spec.containers[*].name' --no-headers"
    return _run_on_cluster(control_plane_node, command, profile=profile, timeout=30)
