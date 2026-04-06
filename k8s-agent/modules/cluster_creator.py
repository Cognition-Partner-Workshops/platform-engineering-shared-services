"""Cluster Creator — SSH-based K8s cluster provisioning with CRI-O + Flannel."""

import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from modules.llm_client import query_llm
from modules.profile_manager import ClusterProfile


@dataclass
class SSHResult:
    """Result of an SSH command execution."""

    hostname: str
    command: str
    return_code: int
    stdout: str
    stderr: str
    success: bool


def run_ssh_command(
    ip_address: str,
    command: str,
    ssh_user: str = "root",
    ssh_port: int = 22,
    ssh_key_path: str = "~/.ssh/id_rsa",
    timeout: int = 600,
) -> SSHResult:
    """Execute a command on a remote node via SSH.

    Args:
        ip_address: Target node IP.
        command: Shell command to execute remotely.
        ssh_user: SSH username.
        ssh_port: SSH port number.
        ssh_key_path: Path to SSH private key.
        timeout: Command timeout in seconds.

    Returns:
        SSHResult with command output and status.
    """
    ssh_cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-i", ssh_key_path,
        "-p", str(ssh_port),
        f"{ssh_user}@{ip_address}",
        command,
    ]

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return SSHResult(
            hostname=ip_address,
            command=command,
            return_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            success=result.returncode == 0,
        )
    except subprocess.TimeoutExpired:
        return SSHResult(
            hostname=ip_address,
            command=command,
            return_code=-1,
            stdout="",
            stderr=f"Command timed out after {timeout}s",
            success=False,
        )
    except Exception as exc:
        return SSHResult(
            hostname=ip_address,
            command=command,
            return_code=-1,
            stdout="",
            stderr=str(exc),
            success=False,
        )


def test_ssh_connectivity(node: dict) -> SSHResult:
    """Test SSH connectivity to a node."""
    return run_ssh_command(
        ip_address=node["ip_address"],
        command="echo 'SSH connection successful' && hostname && uname -r",
        ssh_user=node.get("ssh_user", "root"),
        ssh_port=node.get("ssh_port", 22),
        ssh_key_path=node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=15,
    )


def generate_common_setup_script(profile: ClusterProfile) -> str:
    """Generate the common setup script that runs on ALL nodes (control-plane + workers)."""
    return f"""#!/bin/bash
set -euo pipefail

echo "=== K8s Node Common Setup ==="
echo "Kubernetes Version: {profile.kubernetes_version}"
echo "CRI-O Version: {profile.crio_version}"
echo "Timestamp: $(date -u)"

# ── 1. System prerequisites ──────────────────────────────────────────────
echo ">> Disabling swap..."
swapoff -a
sed -i '/\\bswap\\b/d' /etc/fstab

echo ">> Loading kernel modules..."
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter

echo ">> Setting sysctl parameters..."
cat > /etc/sysctl.d/99-kubernetes.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system

echo ">> Disabling SELinux (if present)..."
if command -v setenforce &>/dev/null; then
    setenforce 0 || true
    sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config 2>/dev/null || true
fi

echo ">> Configuring firewalld (if present)..."
if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-port=6443/tcp    # API server
    firewall-cmd --permanent --add-port=2379-2380/tcp  # etcd
    firewall-cmd --permanent --add-port=10250/tcp   # Kubelet API
    firewall-cmd --permanent --add-port=10259/tcp   # kube-scheduler
    firewall-cmd --permanent --add-port=10257/tcp   # kube-controller-manager
    firewall-cmd --permanent --add-port=30000-32767/tcp  # NodePort
    firewall-cmd --permanent --add-port=8472/udp    # Flannel VXLAN
    firewall-cmd --reload
fi

# ── 2. Install CRI-O ─────────────────────────────────────────────────────
echo ">> Installing CRI-O {profile.crio_version}..."

OS="$(. /etc/os-release && echo "$ID")"
VERSION_ID="$(. /etc/os-release && echo "$VERSION_ID")"

if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
    apt-get update -y
    apt-get install -y software-properties-common curl gnupg2

    CRIO_VERSION="{profile.crio_version}"
    curl -fsSL "https://pkgs.k8s.io/addons:/cri-o:/stable:/v$CRIO_VERSION/deb/Release.key" | \\
        gpg --dearmor -o /etc/apt/keyrings/cri-o-apt-keyring.gpg
    echo "deb [signed-by=/etc/apt/keyrings/cri-o-apt-keyring.gpg] https://pkgs.k8s.io/addons:/cri-o:/stable:/v$CRIO_VERSION/deb/ /" | \\
        tee /etc/apt/sources.list.d/cri-o.list

    apt-get update -y
    apt-get install -y cri-o
elif [[ "$OS" == "rhel" || "$OS" == "centos" || "$OS" == "rocky" || "$OS" == "almalinux" ]]; then
    CRIO_VERSION="{profile.crio_version}"
    cat > /etc/yum.repos.d/cri-o.repo <<REPO
[cri-o]
name=CRI-O
baseurl=https://pkgs.k8s.io/addons:/cri-o:/stable:/v$CRIO_VERSION/rpm/
enabled=1
gpgcheck=1
gpgkey=https://pkgs.k8s.io/addons:/cri-o:/stable:/v$CRIO_VERSION/rpm/repodata/repomd.xml.key
REPO
    dnf install -y cri-o
fi

systemctl daemon-reload
systemctl enable --now crio
echo ">> CRI-O installed and running."

# ── 3. Install kubeadm, kubelet, kubectl ──────────────────────────────────
echo ">> Installing Kubernetes {profile.kubernetes_version} components..."

K8S_VERSION="{profile.kubernetes_version}"

if [[ "$OS" == "ubuntu" || "$OS" == "debian" ]]; then
    curl -fsSL "https://pkgs.k8s.io/core:/stable:/v$K8S_VERSION/deb/Release.key" | \\
        gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
    echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v$K8S_VERSION/deb/ /" | \\
        tee /etc/apt/sources.list.d/kubernetes.list

    apt-get update -y
    apt-get install -y kubelet kubeadm kubectl
    apt-mark hold kubelet kubeadm kubectl
elif [[ "$OS" == "rhel" || "$OS" == "centos" || "$OS" == "rocky" || "$OS" == "almalinux" ]]; then
    cat > /etc/yum.repos.d/kubernetes.repo <<REPO
[kubernetes]
name=Kubernetes
baseurl=https://pkgs.k8s.io/core:/stable:/v$K8S_VERSION/rpm/
enabled=1
gpgcheck=1
gpgkey=https://pkgs.k8s.io/core:/stable:/v$K8S_VERSION/rpm/repodata/repomd.xml.key
REPO
    dnf install -y kubelet kubeadm kubectl
fi

systemctl enable --now kubelet
echo ">> Kubernetes components installed."

echo "=== Common setup complete ==="
"""


def generate_control_plane_init_script(profile: ClusterProfile) -> str:
    """Generate the kubeadm init script for the control-plane node."""
    cp_nodes = profile.get_control_plane_nodes()
    cp_ip = cp_nodes[0]["ip_address"] if cp_nodes else "CONTROL_PLANE_IP"

    return f"""#!/bin/bash
set -euo pipefail

echo "=== Initializing Kubernetes Control Plane ==="

# ── kubeadm init ──────────────────────────────────────────────────────────
cat > /tmp/kubeadm-config.yaml <<EOF
apiVersion: kubeadm.k8s.io/v1beta3
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: "{cp_ip}"
  bindPort: 6443
nodeRegistration:
  criSocket: "unix:///var/run/crio/crio.sock"
  kubeletExtraArgs:
    container-runtime-endpoint: "unix:///var/run/crio/crio.sock"
---
apiVersion: kubeadm.k8s.io/v1beta3
kind: ClusterConfiguration
kubernetesVersion: "v{profile.kubernetes_version}.0"
networking:
  podSubnet: "{profile.pod_cidr}"
  serviceSubnet: "{profile.service_cidr}"
  dnsDomain: "{profile.dns_domain}"
controlPlaneEndpoint: "{cp_ip}:6443"
apiServer:
  extraArgs:
    authorization-mode: "Node,RBAC"
    enable-admission-plugins: "NodeRestriction,PodSecurity"
    audit-log-path: "/var/log/kubernetes/audit.log"
    audit-log-maxage: "30"
    audit-log-maxbackup: "10"
    audit-log-maxsize: "100"
  extraVolumes:
  - name: audit-log
    hostPath: "/var/log/kubernetes"
    mountPath: "/var/log/kubernetes"
    pathType: DirectoryOrCreate
controllerManager:
  extraArgs:
    bind-address: "0.0.0.0"
    terminated-pod-gc-threshold: "100"
scheduler:
  extraArgs:
    bind-address: "0.0.0.0"
etcd:
  local:
    extraArgs:
      listen-metrics-urls: "http://0.0.0.0:2381"
---
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
cgroupDriver: systemd
containerRuntimeEndpoint: "unix:///var/run/crio/crio.sock"
evictionHard:
  memory.available: "100Mi"
  nodefs.available: "10%"
  imagefs.available: "15%"
EOF

echo ">> Running kubeadm init..."
kubeadm init --config=/tmp/kubeadm-config.yaml --upload-certs | tee /tmp/kubeadm-init.log

# ── Configure kubectl for root ────────────────────────────────────────────
echo ">> Configuring kubectl..."
mkdir -p /root/.kube
cp /etc/kubernetes/admin.conf /root/.kube/config
chown root:root /root/.kube/config

# ── Install Flannel CNI ───────────────────────────────────────────────────
echo ">> Installing Flannel CNI..."
kubectl apply -f https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml

# Wait for Flannel to be ready
echo ">> Waiting for Flannel pods to be ready..."
kubectl -n kube-flannel wait --for=condition=ready pod -l app=flannel --timeout=120s || true

# ── Apply Pod Security Standards ──────────────────────────────────────────
echo ">> Applying Pod Security Standards ({profile.pod_security_standard})..."
kubectl label namespace default \\
    pod-security.kubernetes.io/enforce={profile.pod_security_standard} \\
    pod-security.kubernetes.io/warn={profile.pod_security_standard} \\
    pod-security.kubernetes.io/audit={profile.pod_security_standard} \\
    --overwrite

# ── Generate join command ─────────────────────────────────────────────────
echo ">> Generating worker join command..."
kubeadm token create --print-join-command > /tmp/kubeadm-join-command.txt
echo "Join command saved to /tmp/kubeadm-join-command.txt"

echo ""
echo "=== Control Plane initialization complete ==="
echo "Join command:"
cat /tmp/kubeadm-join-command.txt
"""


def generate_worker_join_script() -> str:
    """Generate the script that runs on worker nodes to join the cluster."""
    return """#!/bin/bash
set -euo pipefail

echo "=== Joining Worker Node to Cluster ==="

JOIN_COMMAND="$1"

if [ -z "$JOIN_COMMAND" ]; then
    echo "ERROR: Join command not provided."
    echo "Usage: $0 '<kubeadm join command>'"
    exit 1
fi

echo ">> Executing join command..."
eval "$JOIN_COMMAND --cri-socket unix:///var/run/crio/crio.sock"

echo "=== Worker node joined successfully ==="
"""


def generate_best_practices_script() -> str:
    """Generate a post-install best practices hardening script."""
    return """#!/bin/bash
set -euo pipefail

echo "=== Applying Kubernetes Best Practices ==="

# ── Default Network Policy (deny-all) ────────────────────────────────────
echo ">> Creating default-deny network policy for default namespace..."
cat <<EOF | kubectl apply -f -
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny-all
  namespace: default
spec:
  podSelector: {}
  policyTypes:
  - Ingress
  - Egress
EOF

# ── Resource Quotas for default namespace ─────────────────────────────────
echo ">> Setting resource quotas..."
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ResourceQuota
metadata:
  name: default-quota
  namespace: default
spec:
  hard:
    requests.cpu: "4"
    requests.memory: 8Gi
    limits.cpu: "8"
    limits.memory: 16Gi
    pods: "50"
    services: "20"
    persistentvolumeclaims: "10"
EOF

# ── Limit Ranges ──────────────────────────────────────────────────────────
echo ">> Setting limit ranges..."
cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: LimitRange
metadata:
  name: default-limits
  namespace: default
spec:
  limits:
  - default:
      cpu: "500m"
      memory: "512Mi"
    defaultRequest:
      cpu: "100m"
      memory: "128Mi"
    type: Container
EOF

# ── RBAC: Create read-only ClusterRole ────────────────────────────────────
echo ">> Creating read-only ClusterRole..."
cat <<EOF | kubectl apply -f -
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: cluster-reader
rules:
- apiGroups: [""]
  resources: ["pods", "services", "namespaces", "nodes", "events", "configmaps"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["apps"]
  resources: ["deployments", "replicasets", "statefulsets", "daemonsets"]
  verbs: ["get", "list", "watch"]
- apiGroups: ["networking.k8s.io"]
  resources: ["networkpolicies", "ingresses"]
  verbs: ["get", "list", "watch"]
EOF

# ── Enable audit logging directory ────────────────────────────────────────
echo ">> Ensuring audit log directory exists..."
mkdir -p /var/log/kubernetes

echo "=== Best practices applied ==="
echo ""
echo "Summary of applied best practices:"
echo "  - Default-deny NetworkPolicy in default namespace"
echo "  - ResourceQuota for default namespace (CPU: 4/8, Memory: 8/16Gi)"
echo "  - LimitRange with default container limits"
echo "  - Read-only ClusterRole (cluster-reader)"
echo "  - Audit logging directory configured"
"""


def provision_node_common(node: dict, profile: ClusterProfile) -> SSHResult:
    """Run the common setup script on a single node via SSH."""
    script = generate_common_setup_script(profile)
    return run_ssh_command(
        ip_address=node["ip_address"],
        command=script,
        ssh_user=node.get("ssh_user", "root"),
        ssh_port=node.get("ssh_port", 22),
        ssh_key_path=node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=600,
    )


def init_control_plane(node: dict, profile: ClusterProfile) -> SSHResult:
    """Initialize the control plane on the given node."""
    script = generate_control_plane_init_script(profile)
    return run_ssh_command(
        ip_address=node["ip_address"],
        command=script,
        ssh_user=node.get("ssh_user", "root"),
        ssh_port=node.get("ssh_port", 22),
        ssh_key_path=node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=600,
    )


def retrieve_join_command(control_plane_node: dict) -> Optional[str]:
    """Retrieve the kubeadm join command from the control-plane node."""
    result = run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command="cat /tmp/kubeadm-join-command.txt",
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=30,
    )
    if result.success:
        return result.stdout.strip()
    return None


def join_worker_node(node: dict, join_command: str) -> SSHResult:
    """Join a worker node to the cluster."""
    full_command = f"{join_command} --cri-socket unix:///var/run/crio/crio.sock"
    return run_ssh_command(
        ip_address=node["ip_address"],
        command=full_command,
        ssh_user=node.get("ssh_user", "root"),
        ssh_port=node.get("ssh_port", 22),
        ssh_key_path=node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=300,
    )


def apply_best_practices(control_plane_node: dict) -> SSHResult:
    """Apply best practices hardening on the cluster via the control-plane."""
    script = generate_best_practices_script()
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command=script,
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=120,
    )


def get_cluster_status(control_plane_node: dict) -> SSHResult:
    """Get the cluster node status from the control-plane."""
    return run_ssh_command(
        ip_address=control_plane_node["ip_address"],
        command="kubectl get nodes -o wide && echo '---' && kubectl get pods -A",
        ssh_user=control_plane_node.get("ssh_user", "root"),
        ssh_port=control_plane_node.get("ssh_port", 22),
        ssh_key_path=control_plane_node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=30,
    )


def get_llm_cluster_advice(profile: ClusterProfile, context: str = "") -> str:
    """Ask the LLM for cluster setup advice based on the profile."""
    nodes_desc = []
    for n in profile.nodes:
        nodes_desc.append(f"  - {n.get('hostname', 'unknown')} ({n['ip_address']}) — role: {n['role']}")
    nodes_str = "\n".join(nodes_desc)

    prompt = f"""I am setting up an on-premises Kubernetes cluster with the following configuration:

- Kubernetes Version: {profile.kubernetes_version}
- Container Runtime: CRI-O {profile.crio_version}
- CNI Plugin: Flannel
- Pod CIDR: {profile.pod_cidr}
- Service CIDR: {profile.service_cidr}
- Pod Security Standard: {profile.pod_security_standard}

Nodes:
{nodes_str}

{context}

Please review this configuration and provide:
1. Any potential issues or conflicts
2. Recommended optimizations
3. Security hardening recommendations specific to this setup
4. Network configuration tips for Flannel with CRI-O
"""
    return query_llm(prompt)
