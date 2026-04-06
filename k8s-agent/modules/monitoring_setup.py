"""Monitoring Setup — Prometheus, Grafana, and dashboard provisioning via SSH."""

from modules.cluster_creator import run_ssh_command, SSHResult
from modules.llm_client import query_llm
from modules.profile_manager import ClusterProfile


def generate_helm_install_script() -> str:
    """Generate script to install Helm on the control-plane node."""
    return """#!/bin/bash
set -euo pipefail

echo "=== Installing Helm ==="

if command -v helm &>/dev/null; then
    echo "Helm already installed: $(helm version --short)"
else
    curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
    echo "Helm installed: $(helm version --short)"
fi
"""


def generate_prometheus_install_script(namespace: str = "monitoring") -> str:
    """Generate script to install kube-prometheus-stack via Helm."""
    return f"""#!/bin/bash
set -euo pipefail

echo "=== Installing Prometheus Stack ==="

# Add Helm repos
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo update

# Create namespace
kubectl create namespace {namespace} --dry-run=client -o yaml | kubectl apply -f -

# Install kube-prometheus-stack
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \\
    --namespace {namespace} \\
    --set prometheus.prometheusSpec.retention=15d \\
    --set prometheus.prometheusSpec.resources.requests.memory=512Mi \\
    --set prometheus.prometheusSpec.resources.requests.cpu=250m \\
    --set prometheus.prometheusSpec.resources.limits.memory=2Gi \\
    --set prometheus.prometheusSpec.resources.limits.cpu=1000m \\
    --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes[0]=ReadWriteOnce \\
    --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=50Gi \\
    --set alertmanager.alertmanagerSpec.resources.requests.memory=128Mi \\
    --set alertmanager.alertmanagerSpec.resources.requests.cpu=50m \\
    --set grafana.enabled=true \\
    --set grafana.adminPassword=admin \\
    --set grafana.persistence.enabled=true \\
    --set grafana.persistence.size=10Gi \\
    --set grafana.resources.requests.memory=256Mi \\
    --set grafana.resources.requests.cpu=100m \\
    --set grafana.resources.limits.memory=512Mi \\
    --set grafana.resources.limits.cpu=500m \\
    --set grafana.sidecar.dashboards.enabled=true \\
    --set grafana.sidecar.dashboards.searchNamespace=ALL \\
    --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \\
    --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \\
    --wait --timeout 10m

echo ""
echo "=== Prometheus Stack installed ==="
echo ""
kubectl -n {namespace} get pods
"""


def generate_standalone_grafana_script(namespace: str = "monitoring") -> str:
    """Generate script to install standalone Grafana with provisioned dashboards."""
    return f"""#!/bin/bash
set -euo pipefail

echo "=== Installing Standalone Grafana ==="

helm repo add grafana https://grafana.github.io/helm-charts 2>/dev/null || true
helm repo update

kubectl create namespace {namespace} --dry-run=client -o yaml | kubectl apply -f -

helm upgrade --install grafana grafana/grafana \\
    --namespace {namespace} \\
    --set adminPassword=admin \\
    --set persistence.enabled=true \\
    --set persistence.size=10Gi \\
    --set resources.requests.memory=256Mi \\
    --set resources.requests.cpu=100m \\
    --set resources.limits.memory=512Mi \\
    --set resources.limits.cpu=500m \\
    --set sidecar.dashboards.enabled=true \\
    --set sidecar.dashboards.searchNamespace=ALL \\
    --set sidecar.datasources.enabled=true \\
    --set 'datasources.datasources\\.yaml.apiVersion=1' \\
    --set 'datasources.datasources\\.yaml.datasources[0].name=Prometheus' \\
    --set 'datasources.datasources\\.yaml.datasources[0].type=prometheus' \\
    --set 'datasources.datasources\\.yaml.datasources[0].url=http://prometheus-kube-prometheus-prometheus.{namespace}.svc:9090' \\
    --set 'datasources.datasources\\.yaml.datasources[0].access=proxy' \\
    --set 'datasources.datasources\\.yaml.datasources[0].isDefault=true' \\
    --wait --timeout 5m

echo ""
echo "=== Grafana installed ==="
kubectl -n {namespace} get pods -l app.kubernetes.io/name=grafana
"""


GRAFANA_DASHBOARDS = {
    "cluster-overview": {
        "name": "Kubernetes Cluster Overview",
        "description": "Overall cluster health, node status, resource utilization",
        "gnet_id": 15520,
    },
    "node-exporter": {
        "name": "Node Exporter Full",
        "description": "Detailed node metrics — CPU, memory, disk, network",
        "gnet_id": 1860,
    },
    "pod-monitoring": {
        "name": "Kubernetes Pods",
        "description": "Pod-level CPU, memory, network, restarts",
        "gnet_id": 15760,
    },
    "namespace-resources": {
        "name": "Namespace Resources",
        "description": "Resource usage per namespace with quota tracking",
        "gnet_id": 15758,
    },
    "coredns": {
        "name": "CoreDNS",
        "description": "DNS query rates, latency, errors",
        "gnet_id": 15762,
    },
    "etcd": {
        "name": "etcd",
        "description": "etcd cluster health, leader changes, WAL sync duration",
        "gnet_id": 3070,
    },
    "api-server": {
        "name": "Kubernetes API Server",
        "description": "API server request rates, latency, errors",
        "gnet_id": 15761,
    },
    "persistent-volumes": {
        "name": "Persistent Volumes",
        "description": "PV/PVC usage and capacity tracking",
        "gnet_id": 13646,
    },
}


def generate_dashboard_import_script(
    dashboard_keys: list[str],
    namespace: str = "monitoring",
) -> str:
    """Generate script to import Grafana dashboards as ConfigMaps."""
    configmaps = []
    for key in dashboard_keys:
        dash = GRAFANA_DASHBOARDS.get(key)
        if not dash:
            continue
        configmaps.append(f"""
# Import: {dash['name']}
cat <<'DASHEOF' | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: grafana-dashboard-{key}
  namespace: {namespace}
  labels:
    grafana_dashboard: "1"
data:
  {key}.json: |
    {{
      "annotations": {{"list": []}},
      "description": "{dash['description']}",
      "editable": true,
      "gnetId": {dash['gnet_id']},
      "title": "{dash['name']}",
      "uid": "{key}",
      "version": 1,
      "__inputs": [
        {{
          "name": "DS_PROMETHEUS",
          "label": "Prometheus",
          "type": "datasource",
          "pluginId": "prometheus"
        }}
      ]
    }}
DASHEOF
echo "  Imported: {dash['name']} (grafana.net #{dash['gnet_id']})"
""")

    script_body = "\n".join(configmaps)

    return f"""#!/bin/bash
set -euo pipefail

echo "=== Importing Grafana Dashboards ==="
{script_body}

# Also import from grafana.net directly via Grafana API
GRAFANA_POD=$(kubectl -n {namespace} get pods -l app.kubernetes.io/name=grafana -o jsonpath='{{.items[0].metadata.name}}' 2>/dev/null || echo "")

if [ -n "$GRAFANA_POD" ]; then
    echo ""
    echo ">> Importing full dashboards from grafana.net via API..."
    kubectl -n {namespace} port-forward "$GRAFANA_POD" 3000:3000 &
    PF_PID=$!
    sleep 3

    for gnet_id in {' '.join(str(GRAFANA_DASHBOARDS[k]['gnet_id']) for k in dashboard_keys if k in GRAFANA_DASHBOARDS)}; do
        curl -s -X POST http://localhost:3000/api/dashboards/import \\
            -H "Content-Type: application/json" \\
            -u admin:admin \\
            -d "{{
                \\"dashboard\\": {{\\"id\\": null}},
                \\"overwrite\\": true,
                \\"inputs\\": [{{\\"name\\": \\"DS_PROMETHEUS\\", \\"type\\": \\"datasource\\", \\"pluginId\\": \\"prometheus\\", \\"value\\": \\"Prometheus\\"}}],
                \\"folderId\\": 0,
                \\"gnetId\\": $gnet_id
            }}" 2>/dev/null && echo "  Imported grafana.net #$gnet_id" || echo "  Failed grafana.net #$gnet_id (non-critical)"
    done

    kill $PF_PID 2>/dev/null || true
fi

echo ""
echo "=== Dashboard import complete ==="
"""


def generate_alerting_rules_script(namespace: str = "monitoring") -> str:
    """Generate PrometheusRule resources for common K8s alerts."""
    return f"""#!/bin/bash
set -euo pipefail

echo "=== Installing Alert Rules ==="

cat <<'EOF' | kubectl apply -f -
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: k8s-cluster-alerts
  namespace: {namespace}
  labels:
    release: prometheus
spec:
  groups:
  - name: k8s-node-alerts
    rules:
    - alert: NodeNotReady
      expr: kube_node_status_condition{{condition="Ready",status="true"}} == 0
      for: 5m
      labels:
        severity: critical
      annotations:
        summary: "Node {{{{ $labels.node }}}} is not ready"
        description: "Node has been in NotReady state for more than 5 minutes."
    - alert: NodeHighCPU
      expr: 100 - (avg by(instance) (rate(node_cpu_seconds_total{{mode="idle"}}[5m])) * 100) > 85
      for: 10m
      labels:
        severity: warning
      annotations:
        summary: "Node {{{{ $labels.instance }}}} has high CPU usage"
        description: "CPU usage is above 85% for more than 10 minutes."
    - alert: NodeHighMemory
      expr: (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100 > 85
      for: 10m
      labels:
        severity: warning
      annotations:
        summary: "Node {{{{ $labels.instance }}}} has high memory usage"
        description: "Memory usage is above 85% for more than 10 minutes."
    - alert: NodeDiskPressure
      expr: (1 - node_filesystem_avail_bytes{{mountpoint="/"}} / node_filesystem_size_bytes{{mountpoint="/"}}) * 100 > 85
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "Node {{{{ $labels.instance }}}} disk usage is high"
        description: "Root filesystem usage is above 85%."
  - name: k8s-pod-alerts
    rules:
    - alert: PodCrashLooping
      expr: rate(kube_pod_container_status_restarts_total[15m]) * 60 * 15 > 5
      for: 5m
      labels:
        severity: critical
      annotations:
        summary: "Pod {{{{ $labels.namespace }}}}/{{{{ $labels.pod }}}} is crash looping"
        description: "Pod has restarted more than 5 times in the last 15 minutes."
    - alert: PodNotReady
      expr: kube_pod_status_ready{{condition="true"}} == 0
      for: 10m
      labels:
        severity: warning
      annotations:
        summary: "Pod {{{{ $labels.namespace }}}}/{{{{ $labels.pod }}}} is not ready"
        description: "Pod has been in a non-ready state for more than 10 minutes."
    - alert: PVCAlmostFull
      expr: kubelet_volume_stats_used_bytes / kubelet_volume_stats_capacity_bytes * 100 > 85
      for: 5m
      labels:
        severity: warning
      annotations:
        summary: "PVC {{{{ $labels.persistentvolumeclaim }}}} is almost full"
        description: "PVC in namespace {{{{ $labels.namespace }}}} is over 85% full."
  - name: k8s-etcd-alerts
    rules:
    - alert: EtcdHighLatency
      expr: histogram_quantile(0.99, rate(etcd_disk_wal_fsync_duration_seconds_bucket[5m])) > 0.5
      for: 10m
      labels:
        severity: warning
      annotations:
        summary: "etcd WAL fsync latency is high"
        description: "99th percentile etcd WAL fsync duration exceeds 500ms."
EOF

echo "=== Alert rules installed ==="
"""


def install_helm(control_plane_node: dict) -> SSHResult:
    """Install Helm on the control-plane node."""
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=generate_helm_install_script(),
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=120,
    )


def install_prometheus_stack(
    control_plane_node: dict,
    namespace: str = "monitoring",
) -> SSHResult:
    """Install the full kube-prometheus-stack."""
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=generate_prometheus_install_script(namespace),
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=900,
    )


def install_dashboards(
    control_plane_node: dict,
    dashboard_keys: list[str],
    namespace: str = "monitoring",
) -> SSHResult:
    """Import selected Grafana dashboards."""
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=generate_dashboard_import_script(dashboard_keys, namespace),
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=300,
    )


def install_alert_rules(
    control_plane_node: dict,
    namespace: str = "monitoring",
) -> SSHResult:
    """Install Prometheus alerting rules."""
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=generate_alerting_rules_script(namespace),
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=60,
    )


def get_monitoring_status(
    control_plane_node: dict,
    namespace: str = "monitoring",
) -> SSHResult:
    """Check the status of the monitoring stack."""
    command = f"""
echo "=== Monitoring Stack Status ==="
echo ""
echo ">> Pods:"
kubectl -n {namespace} get pods -o wide
echo ""
echo ">> Services:"
kubectl -n {namespace} get svc
echo ""
echo ">> PVCs:"
kubectl -n {namespace} get pvc
echo ""
echo ">> PrometheusRules:"
kubectl -n {namespace} get prometheusrules 2>/dev/null || echo "No PrometheusRules found"
echo ""
echo ">> ServiceMonitors:"
kubectl -n {namespace} get servicemonitors 2>/dev/null || echo "No ServiceMonitors found"
"""
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=command,
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=30,
    )


def get_monitoring_advice(
    profile: ClusterProfile,
    current_status: str = "",
) -> str:
    """Ask the LLM for monitoring setup advice."""
    prompt = f"""I have a Kubernetes cluster with the following setup:
- Kubernetes: {profile.kubernetes_version}
- Runtime: CRI-O {profile.crio_version}
- CNI: Flannel
- Nodes: {len(profile.nodes)} ({len(profile.get_control_plane_nodes())} control-plane, {len(profile.get_worker_nodes())} workers)

Current monitoring status:
{current_status or 'Not yet installed'}

Please recommend:
1. The optimal Prometheus retention and resource settings for this cluster size
2. Essential Grafana dashboards to install
3. Critical alerting rules beyond the standard set
4. Any additional exporters I should install (e.g., blackbox, SNMP)
5. Log aggregation recommendations (Loki, EFK, etc.)
"""
    return query_llm(prompt)
