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
    """Run a shell command locally with KUBECONFIG set from profile content.

    Replaces bare ``kubectl`` and ``helm`` references with their full paths
    so the command works even when these binaries are not in $PATH.
    """
    kubectl = config.get_kubectl_path()
    helm = config.get_helm_path()
    if not kubectl:
        return SSHResult(
            hostname="local", command=command, return_code=1,
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
    # Replace bare kubectl/helm with full paths
    resolved_cmd = command.replace("kubectl ", f"{kubectl} ").replace("helm ", f"{helm} " if helm else "helm ")
    kubeconfig_path = config.get_kubeconfig_path("_log_temp")
    with open(kubeconfig_path, "w") as f:
        f.write(kubeconfig_content)
    env = dict(os.environ, KUBECONFIG=kubeconfig_path)
    try:
        proc = subprocess.run(resolved_cmd, shell=True, capture_output=True, text=True, timeout=timeout, env=env)
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

    def _parse_ts(ts_str: str):
        """Try to parse a timestamp string into a datetime object."""
        from datetime import datetime
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%SZ",
            "%Y-%m-%dT%H:%M:%S.%fZ",
            "%b %d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(ts_str.strip(), fmt)
            except (ValueError, AttributeError):
                continue
        return None

    correlated = []
    window_seconds = 30
    used: set[int] = set()

    for i, err in enumerate(all_errors):
        if i in used:
            continue
        group = [err]
        used.add(i)
        err_ts = _parse_ts(err.get("timestamp", ""))

        for j in range(i + 1, len(all_errors)):
            if j in used:
                continue
            other = all_errors[j]
            if other.get("source") == err.get("source"):
                continue
            # If both timestamps are parseable, enforce the time window
            other_ts = _parse_ts(other.get("timestamp", ""))
            if err_ts and other_ts:
                diff = abs((other_ts - err_ts).total_seconds())
                if diff > window_seconds:
                    continue
            group.append(other)
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


# ══════════════════════════════════════════════════════════════════════════
#  LogAI-inspired Smart Log Analysis
#  Provides: Log Clustering, Anomaly Detection, Pattern Mining, Summarization
#  Uses scikit-learn (TF-IDF + DBSCAN) instead of LogAI directly due to
#  Python 3.12 compatibility issues with the logai package.
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class LogCluster:
    """A cluster of similar log messages."""
    cluster_id: int
    template: str
    count: int
    level: str  # predominant level: ERROR, WARNING, INFO
    sample_messages: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_seen: str = ""


@dataclass
class LogAnomaly:
    """An anomalous log line or pattern."""
    message: str
    score: float  # anomaly score (higher = more anomalous)
    reason: str
    timestamp: str = ""
    source: str = ""


@dataclass
class IstioAccessEntry:
    """A parsed Istio/Envoy access log entry."""
    timestamp: str = ""
    method: str = ""
    path: str = ""
    protocol: str = ""
    response_code: int = 0
    response_flags: str = ""
    bytes_received: int = 0
    bytes_sent: int = 0
    duration_ms: float = 0.0          # total request duration
    upstream_service_time_ms: float = 0.0  # time spent in upstream
    upstream_cluster: str = ""
    upstream_host: str = ""
    downstream_remote: str = ""
    downstream_local: str = ""
    requested_server_name: str = ""
    authority: str = ""               # Host header
    user_agent: str = ""
    raw_line: str = ""


@dataclass
class IstioAnalysisResult:
    """Result from Istio access log analysis."""
    total_requests: int = 0
    parsed_entries: list[IstioAccessEntry] = field(default_factory=list)
    # Latency percentiles
    p50_ms: float = 0.0
    p90_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    avg_ms: float = 0.0
    max_ms: float = 0.0
    min_ms: float = 0.0
    # Status code distribution
    status_distribution: dict = field(default_factory=dict)   # code -> count
    status_class_distribution: dict = field(default_factory=dict)  # "2xx"->count
    # Error rate
    error_rate: float = 0.0  # percentage of 4xx+5xx
    # Slow requests (above p95)
    slow_requests: list[IstioAccessEntry] = field(default_factory=list)
    # Per-path stats
    path_stats: list[dict] = field(default_factory=list)
    # Per-upstream stats
    upstream_stats: list[dict] = field(default_factory=list)
    # Response flags distribution
    response_flags_dist: dict = field(default_factory=dict)
    # Timeline buckets (per-minute)
    timeline_buckets: list[dict] = field(default_factory=list)


@dataclass
class SmartAnalysisResult:
    """Full result from smart log analysis."""
    total_lines: int = 0
    clusters: list[LogCluster] = field(default_factory=list)
    anomalies: list[LogAnomaly] = field(default_factory=list)
    patterns: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    timeline_buckets: list[dict] = field(default_factory=list)
    istio: IstioAnalysisResult | None = None  # populated when Istio logs detected


def _tokenize_log(message: str) -> str:
    """Tokenize a log message by replacing variable parts with placeholders.

    This mimics LogAI's Drain-style log parsing — variable tokens (IPs,
    hex IDs, numbers, paths, UUIDs) are replaced so that messages with the
    same *template* look identical after tokenization.
    """
    # Remove leading timestamp (various formats)
    msg = re.sub(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[^\s]*\s*", "", message)
    msg = re.sub(r"^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s*", "", msg)
    # Replace UUIDs
    msg = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<UUID>", msg, flags=re.IGNORECASE)
    # Replace hex IDs (8+ chars)
    msg = re.sub(r"\b[0-9a-f]{8,}\b", "<HEX>", msg)
    # Replace IPs
    msg = re.sub(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?", "<IP>", msg)
    # Replace pure numbers
    msg = re.sub(r"\b\d+\b", "<NUM>", msg)
    # Replace file paths
    msg = re.sub(r"/[\w./-]+", "<PATH>", msg)
    # Replace pod/container names with common suffixes
    msg = re.sub(r"\b[\w]+-[0-9a-f]{5,10}\b", "<POD>", msg)
    return msg.strip()


def cluster_logs(log_text: str, source: str = "", max_clusters: int = 50, eps: float = 0.5) -> list[LogCluster]:
    """Cluster log messages using TF-IDF vectorization + DBSCAN.

    Inspired by LogAI's log clustering pipeline:
    1. Parse each log line
    2. Tokenize to extract log templates
    3. Vectorize with TF-IDF
    4. Cluster with DBSCAN (density-based — no need to specify k)
    5. Return clusters sorted by size
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.cluster import DBSCAN
        import numpy as np
    except ImportError:
        return []

    lines = [l.strip() for l in log_text.strip().split("\n") if l.strip()]
    if len(lines) < 3:
        return []

    # Parse and tokenize
    entries = [parse_log_line(l, source) for l in lines]
    tokenized = [_tokenize_log(e.message) for e in entries]

    # Filter out empty tokenized lines
    valid_indices = [i for i, t in enumerate(tokenized) if t.strip()]
    if len(valid_indices) < 3:
        return []

    valid_tokenized = [tokenized[i] for i in valid_indices]
    valid_entries = [entries[i] for i in valid_indices]

    # TF-IDF vectorization
    try:
        vectorizer = TfidfVectorizer(max_features=1000, stop_words=None, token_pattern=r"(?u)\b\w+\b")
        tfidf_matrix = vectorizer.fit_transform(valid_tokenized)
    except ValueError:
        return []

    # DBSCAN clustering
    clustering = DBSCAN(eps=eps, min_samples=2, metric="cosine")
    labels = clustering.fit_predict(tfidf_matrix)

    # Build clusters
    cluster_map: dict[int, list[int]] = {}
    for idx, label in enumerate(labels):
        cluster_map.setdefault(label, []).append(idx)

    result_clusters = []
    for cluster_id, member_indices in sorted(cluster_map.items(), key=lambda x: -len(x[1])):
        members = [valid_entries[i] for i in member_indices]
        levels = [m.level for m in members]
        level_counter = Counter(levels)
        predominant_level = level_counter.most_common(1)[0][0]

        # Use the most common tokenized form as the template
        templates = [valid_tokenized[i] for i in member_indices]
        template = Counter(templates).most_common(1)[0][0]

        # Timestamps
        timestamps = [m.timestamp for m in members if m.timestamp]
        first_seen = min(timestamps) if timestamps else ""
        last_seen = max(timestamps) if timestamps else ""

        samples = [members[i].raw for i in range(min(3, len(members)))]

        label_str = "noise" if cluster_id == -1 else str(cluster_id)
        result_clusters.append(LogCluster(
            cluster_id=cluster_id,
            template=template if cluster_id != -1 else "(unclustered / unique messages)",
            count=len(members),
            level=predominant_level,
            sample_messages=samples,
            first_seen=first_seen,
            last_seen=last_seen,
        ))

    # Sort by count descending, but put noise cluster (-1) last
    result_clusters.sort(key=lambda c: (c.cluster_id == -1, -c.count))
    return result_clusters[:max_clusters]


def detect_anomalies(log_text: str, source: str = "", threshold: float = 2.0) -> list[LogAnomaly]:
    """Detect anomalous log lines using frequency-based and TF-IDF outlier detection.

    Inspired by LogAI's anomaly detection pipeline:
    1. Tokenize messages to get templates
    2. Count template frequencies
    3. Rare templates (below frequency threshold) are flagged
    4. Additionally, use TF-IDF distance from centroid for outlier scoring
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        import numpy as np
    except ImportError:
        return []

    lines = [l.strip() for l in log_text.strip().split("\n") if l.strip()]
    if len(lines) < 5:
        return []

    entries = [parse_log_line(l, source) for l in lines]
    tokenized = [_tokenize_log(e.message) for e in entries]

    # Frequency-based anomaly detection
    template_counts = Counter(tokenized)
    total = len(tokenized)
    freq_threshold = max(1, total * 0.01)  # templates appearing in < 1% of lines

    anomalies = []

    # TF-IDF outlier detection
    try:
        vectorizer = TfidfVectorizer(max_features=500, token_pattern=r"(?u)\b\w+\b")
        tfidf_matrix = vectorizer.fit_transform(tokenized)
        centroid = tfidf_matrix.mean(axis=0)
        centroid = np.asarray(centroid).flatten()

        distances = []
        for i in range(tfidf_matrix.shape[0]):
            vec = np.asarray(tfidf_matrix[i].todense()).flatten()
            dist = np.linalg.norm(vec - centroid)
            distances.append(dist)

        distances = np.array(distances)
        mean_dist = distances.mean()
        std_dist = distances.std() if distances.std() > 0 else 1.0

        for i, (entry, dist) in enumerate(zip(entries, distances)):
            z_score = (dist - mean_dist) / std_dist
            reasons = []

            # TF-IDF outlier
            if z_score > threshold:
                reasons.append(f"TF-IDF outlier (z-score: {z_score:.2f})")

            # Frequency anomaly
            if template_counts[tokenized[i]] <= freq_threshold:
                reasons.append(f"Rare template (seen {template_counts[tokenized[i]]}x out of {total})")

            # Error/critical level
            if entry.level == "ERROR":
                reasons.append("Error-level message")

            if reasons:
                anomalies.append(LogAnomaly(
                    message=entry.raw,
                    score=float(z_score),
                    reason="; ".join(reasons),
                    timestamp=entry.timestamp,
                    source=source,
                ))
    except ValueError:
        # Fallback to frequency-only if TF-IDF fails
        for i, entry in enumerate(entries):
            if template_counts[tokenized[i]] <= freq_threshold:
                anomalies.append(LogAnomaly(
                    message=entry.raw,
                    score=1.0,
                    reason=f"Rare template (seen {template_counts[tokenized[i]]}x out of {total})",
                    timestamp=entry.timestamp,
                    source=source,
                ))

    # Sort by score descending
    anomalies.sort(key=lambda a: -a.score)
    return anomalies[:100]  # cap at 100


def mine_log_patterns(log_text: str, source: str = "", top_n: int = 30) -> list[dict]:
    """Mine frequent log patterns/templates from log text.

    Inspired by LogAI's Drain log parser — extracts common templates by
    tokenizing variable parts and counting occurrences.
    """
    lines = [l.strip() for l in log_text.strip().split("\n") if l.strip()]
    if not lines:
        return []

    entries = [parse_log_line(l, source) for l in lines]
    tokenized = [_tokenize_log(e.message) for e in entries]

    # Count templates
    template_counts = Counter(tokenized)

    # Group by template
    template_levels: dict[str, Counter] = {}
    template_samples: dict[str, str] = {}
    for entry, template in zip(entries, tokenized):
        if template not in template_levels:
            template_levels[template] = Counter()
            template_samples[template] = entry.raw
        template_levels[template][entry.level] += 1

    patterns = []
    for template, count in template_counts.most_common(top_n):
        level_dist = dict(template_levels.get(template, {}))
        predominant = max(level_dist, key=level_dist.get) if level_dist else "INFO"
        patterns.append({
            "template": template,
            "count": count,
            "percentage": round(count / len(lines) * 100, 1),
            "level": predominant,
            "level_distribution": level_dist,
            "sample": template_samples.get(template, ""),
        })

    return patterns


def summarize_logs(log_text: str, source: str = "") -> dict:
    """Generate a comprehensive summary of log data.

    Inspired by LogAI's summarization — provides:
    - Level distribution (INFO/WARNING/ERROR counts)
    - Time span
    - Top error messages
    - Log velocity (lines per minute)
    - Health score
    """
    lines = [l.strip() for l in log_text.strip().split("\n") if l.strip()]
    if not lines:
        return {"total_lines": 0, "health_score": 100}

    entries = [parse_log_line(l, source) for l in lines]

    # Level distribution
    levels = Counter(e.level for e in entries)
    error_count = levels.get("ERROR", 0)
    warning_count = levels.get("WARNING", 0)
    info_count = levels.get("INFO", 0)

    # Time span
    timestamps = [e.timestamp for e in entries if e.timestamp]
    first_ts = min(timestamps) if timestamps else "N/A"
    last_ts = max(timestamps) if timestamps else "N/A"

    # Top errors
    error_messages = [_normalize_error(e.message) for e in entries if e.level == "ERROR"]
    top_errors = Counter(error_messages).most_common(10)

    # Top warnings
    warning_messages = [_normalize_error(e.message) for e in entries if e.level == "WARNING"]
    top_warnings = Counter(warning_messages).most_common(5)

    # Unique templates
    tokenized = [_tokenize_log(e.message) for e in entries]
    unique_templates = len(set(tokenized))

    # Health score (0-100)
    # High errors = low score, high warnings = moderate reduction
    error_ratio = error_count / len(lines) if lines else 0
    warning_ratio = warning_count / len(lines) if lines else 0
    health_score = max(0, min(100, int(100 - error_ratio * 300 - warning_ratio * 50)))

    return {
        "total_lines": len(lines),
        "error_count": error_count,
        "warning_count": warning_count,
        "info_count": info_count,
        "level_distribution": dict(levels),
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "top_errors": top_errors,
        "top_warnings": top_warnings,
        "unique_templates": unique_templates,
        "template_diversity": round(unique_templates / len(lines) * 100, 1) if lines else 0,
        "health_score": health_score,
    }


def smart_analyze(log_text: str, source: str = "") -> SmartAnalysisResult:
    """Run the full LogAI-inspired analysis pipeline.

    Combines: clustering, anomaly detection, pattern mining, and summarization.
    """
    result = SmartAnalysisResult()
    lines = [l.strip() for l in log_text.strip().split("\n") if l.strip()]
    result.total_lines = len(lines)

    if not lines:
        return result

    # 1. Clustering
    result.clusters = cluster_logs(log_text, source)

    # 2. Anomaly detection
    result.anomalies = detect_anomalies(log_text, source)

    # 3. Pattern mining
    result.patterns = mine_log_patterns(log_text, source)

    # 4. Summarization
    result.summary = summarize_logs(log_text, source)

    # 5. Timeline buckets (group by timestamp prefix for timeline view)
    entries = [parse_log_line(l, source) for l in lines]
    ts_buckets: dict[str, dict] = {}
    for entry in entries:
        if entry.timestamp:
            # Bucket by minute (first 16 chars: YYYY-MM-DDTHH:MM)
            bucket_key = entry.timestamp[:16] if len(entry.timestamp) >= 16 else entry.timestamp
        else:
            bucket_key = "unknown"
        if bucket_key not in ts_buckets:
            ts_buckets[bucket_key] = {"timestamp": bucket_key, "total": 0, "errors": 0, "warnings": 0}
        ts_buckets[bucket_key]["total"] += 1
        if entry.level == "ERROR":
            ts_buckets[bucket_key]["errors"] += 1
        elif entry.level == "WARNING":
            ts_buckets[bucket_key]["warnings"] += 1

    result.timeline_buckets = sorted(ts_buckets.values(), key=lambda b: b["timestamp"])

    # 6. Istio / Envoy access log analysis (auto-detected)
    istio_result = analyze_istio_access_logs(log_text)
    if istio_result and istio_result.total_requests > 0:
        result.istio = istio_result

    return result


# ══════════════════════════════════════════════════════════════════════════
#  Istio / Envoy Access Log Analysis
#  Parses Envoy access log format used by Istio sidecars and provides
#  response-time analytics, status code distributions, per-path and
#  per-upstream breakdowns, and slow-request detection.
# ══════════════════════════════════════════════════════════════════════════

# Envoy default access log format (as emitted by Istio):
# [%START_TIME%] "%REQ(:METHOD)% %REQ(X-ENVOY-ORIGINAL-PATH?:PATH)% %PROTOCOL%"
# %RESPONSE_CODE% %RESPONSE_FLAGS% %BYTES_RECEIVED% %BYTES_SENT%
# %DURATION% %RESP(X-ENVOY-UPSTREAM-SERVICE-TIME)%
# "%REQ(X-FORWARDED-FOR)%" "%REQ(USER-AGENT)%" "%REQ(X-REQUEST-ID)%"
# "%REQ(:AUTHORITY)%" "%UPSTREAM_HOST%" %UPSTREAM_CLUSTER%
# %UPSTREAM_LOCAL_ADDRESS% %DOWNSTREAM_LOCAL_ADDRESS%
# %DOWNSTREAM_REMOTE_ADDRESS% %REQUESTED_SERVER_NAME% %ROUTE_NAME%

_ISTIO_LOG_RE = re.compile(
    r'\[(?P<timestamp>[^\]]+)\]\s+'
    r'"(?P<method>\S+)\s+(?P<path>\S+)\s+(?P<protocol>[^"]*?)"\s+'
    r'(?P<response_code>\d+)\s+'
    r'(?P<response_flags>\S+)\s+'
    r'(?P<bytes_received>\d+)\s+'
    r'(?P<bytes_sent>\d+)\s+'
    r'(?P<duration>\d+)\s+'
    r'(?P<upstream_service_time>\d+|-)\s+'
    r'"(?P<xff>[^"]*)"\s+'
    r'"(?P<user_agent>[^"]*)"\s+'
    r'"(?P<request_id>[^"]*)"\s+'
    r'"(?P<authority>[^"]*)"\s+'
    r'"(?P<upstream_host>[^"]*)"\s*'
    r'(?P<rest>.*)'
)

# Simpler fallback: JSON-format Istio access logs (structured logging)
_ISTIO_JSON_KEYS = {
    "response_code", "duration", "method", "path", "upstream_service_time",
    "upstream_cluster", "authority", "bytes_received", "bytes_sent",
}


def _parse_istio_line(line: str) -> IstioAccessEntry | None:
    """Try to parse a single line as an Istio/Envoy access log entry."""
    import json as _json

    # Try structured JSON format first
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            obj = _json.loads(stripped)
            # Verify it looks like an Istio access log
            if "response_code" in obj or "method" in obj or "duration" in obj:
                duration = obj.get("duration", 0)
                ust = obj.get("upstream_service_time", 0)
                # Istio JSON logs may use different field names
                return IstioAccessEntry(
                    timestamp=str(obj.get("start_time", obj.get("timestamp", ""))),
                    method=str(obj.get("method", obj.get("request_method", ""))),
                    path=str(obj.get("path", obj.get("request_path", ""))),
                    protocol=str(obj.get("protocol", "")),
                    response_code=int(obj.get("response_code", 0)),
                    response_flags=str(obj.get("response_flags", "-")),
                    bytes_received=int(obj.get("bytes_received", 0)),
                    bytes_sent=int(obj.get("bytes_sent", 0)),
                    duration_ms=float(duration) if duration not in ("-", "", None) else 0.0,
                    upstream_service_time_ms=float(ust) if ust not in ("-", "", None) else 0.0,
                    upstream_cluster=str(obj.get("upstream_cluster", "")),
                    upstream_host=str(obj.get("upstream_host", "")),
                    authority=str(obj.get("authority", obj.get("host", ""))),
                    user_agent=str(obj.get("user_agent", "")),
                    downstream_remote=str(obj.get("downstream_remote_address", "")),
                    downstream_local=str(obj.get("downstream_local_address", "")),
                    requested_server_name=str(obj.get("requested_server_name", "")),
                    raw_line=line,
                )
        except (_json.JSONDecodeError, ValueError, TypeError):
            pass

    # Try standard Envoy text format
    m = _ISTIO_LOG_RE.match(stripped)
    if m:
        ust = m.group("upstream_service_time")
        rest = m.group("rest").strip()
        # Parse remaining fields from rest (upstream_cluster, etc.)
        rest_parts = rest.split()
        upstream_cluster = rest_parts[0] if rest_parts else ""
        return IstioAccessEntry(
            timestamp=m.group("timestamp"),
            method=m.group("method"),
            path=m.group("path"),
            protocol=m.group("protocol"),
            response_code=int(m.group("response_code")),
            response_flags=m.group("response_flags"),
            bytes_received=int(m.group("bytes_received")),
            bytes_sent=int(m.group("bytes_sent")),
            duration_ms=float(m.group("duration")),
            upstream_service_time_ms=float(ust) if ust != "-" else 0.0,
            upstream_cluster=upstream_cluster,
            upstream_host=m.group("upstream_host"),
            authority=m.group("authority"),
            user_agent=m.group("user_agent"),
            downstream_remote=m.group("xff") or "",
            raw_line=line,
        )

    return None


def _is_likely_istio_log(lines: list[str], sample_size: int = 20) -> bool:
    """Heuristic: check if a meaningful fraction of lines look like Istio access logs."""
    sample = lines[:sample_size]
    parsed = sum(1 for l in sample if _parse_istio_line(l) is not None)
    return parsed >= max(1, len(sample) * 0.3)  # at least 30% parse successfully


def analyze_istio_access_logs(log_text: str) -> IstioAnalysisResult | None:
    """Parse and analyze Istio/Envoy access logs.

    Returns None if the logs don't look like Istio access logs.
    Returns an IstioAnalysisResult with latency stats, status distribution,
    per-path breakdowns, per-upstream breakdowns, and slow requests.
    """
    lines = [l.strip() for l in log_text.strip().split("\n") if l.strip()]
    if not lines:
        return None

    # Quick heuristic — bail early if this doesn't look like Istio logs
    if not _is_likely_istio_log(lines):
        return None

    entries: list[IstioAccessEntry] = []
    for line in lines:
        entry = _parse_istio_line(line)
        if entry is not None:
            entries.append(entry)

    if not entries:
        return None

    result = IstioAnalysisResult(
        total_requests=len(entries),
        parsed_entries=entries,
    )

    # ── Latency percentiles ──────────────────────────────────────────
    import numpy as np
    durations = np.array([e.duration_ms for e in entries])
    if len(durations) > 0:
        result.avg_ms = float(np.mean(durations))
        result.min_ms = float(np.min(durations))
        result.max_ms = float(np.max(durations))
        result.p50_ms = float(np.percentile(durations, 50))
        result.p90_ms = float(np.percentile(durations, 90))
        result.p95_ms = float(np.percentile(durations, 95))
        result.p99_ms = float(np.percentile(durations, 99))

    # ── Status code distribution ─────────────────────────────────────
    status_counter: Counter = Counter()
    class_counter: Counter = Counter()
    for e in entries:
        status_counter[e.response_code] += 1
        class_label = f"{e.response_code // 100}xx"
        class_counter[class_label] += 1

    result.status_distribution = dict(status_counter.most_common())
    result.status_class_distribution = dict(class_counter.most_common())

    # Error rate (4xx + 5xx)
    error_count = sum(1 for e in entries if e.response_code >= 400)
    result.error_rate = (error_count / len(entries)) * 100 if entries else 0.0

    # ── Slow requests (above p95) ────────────────────────────────────
    p95_threshold = result.p95_ms
    slow = [e for e in entries if e.duration_ms > p95_threshold]
    # Sort by duration descending, limit to top 50
    slow.sort(key=lambda e: e.duration_ms, reverse=True)
    result.slow_requests = slow[:50]

    # ── Per-path stats ───────────────────────────────────────────────
    path_groups: dict[str, list[IstioAccessEntry]] = {}
    for e in entries:
        # Normalize path: strip query params for grouping
        base_path = e.path.split("?")[0] if e.path else "(unknown)"
        path_groups.setdefault(base_path, []).append(e)

    path_stats = []
    for path, group in path_groups.items():
        durations_g = [e.duration_ms for e in group]
        errors_g = sum(1 for e in group if e.response_code >= 400)
        path_stats.append({
            "path": path,
            "count": len(group),
            "avg_ms": round(sum(durations_g) / len(durations_g), 1) if durations_g else 0,
            "p50_ms": round(float(np.percentile(durations_g, 50)), 1) if durations_g else 0,
            "p95_ms": round(float(np.percentile(durations_g, 95)), 1) if durations_g else 0,
            "p99_ms": round(float(np.percentile(durations_g, 99)), 1) if durations_g else 0,
            "max_ms": round(max(durations_g), 1) if durations_g else 0,
            "error_count": errors_g,
            "error_rate": round((errors_g / len(group)) * 100, 1) if group else 0,
        })
    path_stats.sort(key=lambda p: p["count"], reverse=True)
    result.path_stats = path_stats[:50]

    # ── Per-upstream stats ───────────────────────────────────────────
    upstream_groups: dict[str, list[IstioAccessEntry]] = {}
    for e in entries:
        key = e.upstream_cluster or e.upstream_host or "(direct/unknown)"
        upstream_groups.setdefault(key, []).append(e)

    upstream_stats = []
    for upstream, group in upstream_groups.items():
        ust_vals = [e.upstream_service_time_ms for e in group if e.upstream_service_time_ms > 0]
        dur_vals = [e.duration_ms for e in group]
        errors_g = sum(1 for e in group if e.response_code >= 400)
        upstream_stats.append({
            "upstream": upstream,
            "count": len(group),
            "avg_duration_ms": round(sum(dur_vals) / len(dur_vals), 1) if dur_vals else 0,
            "avg_upstream_ms": round(sum(ust_vals) / len(ust_vals), 1) if ust_vals else 0,
            "p95_duration_ms": round(float(np.percentile(dur_vals, 95)), 1) if dur_vals else 0,
            "p95_upstream_ms": round(float(np.percentile(ust_vals, 95)), 1) if ust_vals else 0,
            "error_count": errors_g,
            "error_rate": round((errors_g / len(group)) * 100, 1) if group else 0,
        })
    upstream_stats.sort(key=lambda u: u["count"], reverse=True)
    result.upstream_stats = upstream_stats[:30]

    # ── Response flags distribution ──────────────────────────────────
    flags_counter: Counter = Counter()
    for e in entries:
        flag = e.response_flags if e.response_flags and e.response_flags != "-" else "(none)"
        flags_counter[flag] += 1
    result.response_flags_dist = dict(flags_counter.most_common())

    # ── Timeline buckets (per-minute) ────────────────────────────────
    ts_buckets: dict[str, dict] = {}
    for e in entries:
        # Try to extract minute-level bucket from timestamp
        ts = e.timestamp
        if ts:
            # Envoy format: 2024-01-15T10:30:45.123Z or similar
            bucket_key = ts[:16] if len(ts) >= 16 else ts[:10]
        else:
            bucket_key = "unknown"
        if bucket_key not in ts_buckets:
            ts_buckets[bucket_key] = {
                "timestamp": bucket_key, "total": 0, "errors": 0,
                "avg_duration": 0.0, "_durations": [],
            }
        ts_buckets[bucket_key]["total"] += 1
        ts_buckets[bucket_key]["_durations"].append(e.duration_ms)
        if e.response_code >= 400:
            ts_buckets[bucket_key]["errors"] += 1

    # Compute avg duration per bucket
    for bucket in ts_buckets.values():
        durs = bucket.pop("_durations", [])
        bucket["avg_duration"] = round(sum(durs) / len(durs), 1) if durs else 0

    result.timeline_buckets = sorted(ts_buckets.values(), key=lambda b: b["timestamp"])

    return result
