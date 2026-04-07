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
class SmartAnalysisResult:
    """Full result from smart log analysis."""
    total_lines: int = 0
    clusters: list[LogCluster] = field(default_factory=list)
    anomalies: list[LogAnomaly] = field(default_factory=list)
    patterns: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    timeline_buckets: list[dict] = field(default_factory=list)


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

    return result
