"""Cluster Creator — SSH-based K8s cluster provisioning with CRI-O + Flannel."""

import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import List, Optional

import config
from modules.profile_manager import ClusterProfile

# Default Flannel manifest URL — can be overridden by user-uploaded file
FLANNEL_MANIFEST_URL = "https://github.com/flannel-io/flannel/releases/latest/download/kube-flannel.yml"


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


def _proxy_env_block(profile: ClusterProfile) -> str:
    """Generate shell export lines for proxy environment variables."""
    lines = []
    proxy = profile.http_proxy or profile.http_proxy_alt
    proxys = profile.https_proxy or profile.https_proxy_alt
    if proxy:
        lines.append(f'export http_proxy="{proxy}"')
        lines.append(f'export HTTP_PROXY="{proxy}"')
    if proxys:
        lines.append(f'export https_proxy="{proxys}"')
        lines.append(f'export HTTPS_PROXY="{proxys}"')
    if profile.no_proxy:
        lines.append(f'export no_proxy="{profile.no_proxy}"')
        lines.append(f'export NO_PROXY="{profile.no_proxy}"')
    return "\n".join(lines)


def generate_common_setup_script(profile: ClusterProfile) -> str:
    """Generate the common setup script that runs on ALL nodes (control-plane + workers)."""
    proxy_block = _proxy_env_block(profile)
    proxy_section = ""
    if proxy_block:
        proxy_section = f"""
# ── 0. Proxy configuration ───────────────────────────────────────────────
echo ">> Configuring proxy settings..."
{proxy_block}

# Persist proxy in /etc/environment for all users
cat >> /etc/environment <<PROXYEOF
{proxy_block}
PROXYEOF
"""

    crio_storage_section = ""
    if profile.crio_root != "/var/lib/containers/storage":
        crio_storage_section = f"""
# ── Custom CRI-O storage paths ───────────────────────────────────────────
echo ">> Configuring CRI-O custom storage root: {profile.crio_root}"
mkdir -p "{profile.crio_root}"
mkdir -p "{profile.crio_runroot}"
"""

    kubelet_section = ""
    if profile.kubelet_root != "/var/lib/kubelet":
        kubelet_section = f"""
# ── Custom kubelet data directory ────────────────────────────────────────
echo ">> Configuring kubelet data directory: {profile.kubelet_root}"
mkdir -p "{profile.kubelet_root}"
"""

    log_section = ""
    if profile.log_root != "/var/log":
        log_section = f"""
# ── Custom log directory ─────────────────────────────────────────────────
echo ">> Configuring custom log root: {profile.log_root}"
mkdir -p "{profile.log_root}/pods"
mkdir -p "{profile.log_root}/containers"
"""

    return f"""#!/bin/bash
set -euo pipefail

echo "=== K8s Node Common Setup ==="
echo "Kubernetes Version: {profile.kubernetes_version}"
echo "CRI-O Version: {profile.crio_version}"
echo "CRI-O Storage Root: {profile.crio_root}"
echo "Kubelet Data Dir: {profile.kubelet_root}"
echo "Log Root: {profile.log_root}"
echo "Timestamp: $(date -u)"
{proxy_section}{crio_storage_section}{kubelet_section}{log_section}
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

# ── Configure CRI-O storage paths ────────────────────────────────────────
echo ">> Configuring CRI-O storage to {profile.crio_root}..."
mkdir -p /etc/crio/crio.conf.d
cat > /etc/crio/crio.conf.d/01-storage.conf <<CRIOCONF
[crio]
  root = "{profile.crio_root}"
  runroot = "{profile.crio_runroot}"
  log_dir = "{profile.log_root}/crio/pods"
CRIOCONF

systemctl enable --now crio
echo ">> CRI-O installed and configured (storage: {profile.crio_root})."

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

    # Build proxy environment block for the control-plane
    proxy_block = _proxy_env_block(profile)
    proxy_section = ""
    if proxy_block:
        proxy_section = f"""
# ── Proxy configuration (master node) ───────────────────────────────────
echo ">> Setting proxy environment for kubeadm..."
{proxy_block}
"""

    # Audit log path respects custom log_root
    audit_log_dir = f"{profile.log_root}/kubernetes"

    # Extra kubelet args for custom root dir
    kubelet_extra = '    container-runtime-endpoint: "unix:///var/run/crio/crio.sock"'
    if profile.kubelet_root != "/var/lib/kubelet":
        kubelet_extra += f'\n    root-dir: "{profile.kubelet_root}"'

    return f"""#!/bin/bash
set -euo pipefail

echo "=== Initializing Kubernetes Control Plane ==="
{proxy_section}
# ── kubeadm init ──────────────────────────────────────────────────────────
mkdir -p "{audit_log_dir}"
cat > /tmp/kubeadm-config.yaml <<EOF
apiVersion: kubeadm.k8s.io/v1beta3
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: "{cp_ip}"
  bindPort: 6443
nodeRegistration:
  criSocket: "unix:///var/run/crio/crio.sock"
  kubeletExtraArgs:
{kubelet_extra}
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
    audit-log-path: "{audit_log_dir}/audit.log"
    audit-log-maxage: "30"
    audit-log-maxbackup: "10"
    audit-log-maxsize: "100"
  extraVolumes:
  - name: audit-log
    hostPath: "{audit_log_dir}"
    mountPath: "{audit_log_dir}"
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
if [ -f /tmp/kube-flannel-custom.yml ]; then
    echo ">> Using user-provided Flannel manifest..."
    kubectl apply -f /tmp/kube-flannel-custom.yml
else
    kubectl apply -f {FLANNEL_MANIFEST_URL}
fi

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


# ══════════════════════════════════════════════════════════════════════════
#  Step-based provisioning — granular SSH execution with per-step progress
# ══════════════════════════════════════════════════════════════════════════


@dataclass
class ProvisionStep:
    """A single discrete provisioning step to be executed over SSH."""

    name: str  # short identifier, e.g. "disable_swap"
    title: str  # human-readable label for the UI
    script: str  # shell snippet to execute
    timeout: int = 300  # per-step timeout in seconds
    fatal: bool = True  # if True, abort provisioning on failure


def _run_step(node: dict, step: ProvisionStep) -> SSHResult:
    """Execute a single ProvisionStep on a node via SSH."""
    return run_ssh_command(
        ip_address=node["ip_address"],
        command=step.script,
        ssh_user=node.get("ssh_user", "root"),
        ssh_port=node.get("ssh_port", 22),
        ssh_key_path=node.get("ssh_key_path", "~/.ssh/id_rsa"),
        timeout=step.timeout,
    )


def get_common_setup_steps(profile: ClusterProfile) -> List[ProvisionStep]:
    """Return the ordered list of discrete steps for common node setup."""
    proxy_block = _proxy_env_block(profile)
    steps: List[ProvisionStep] = []

    # 0. Proxy (optional)
    if proxy_block:
        steps.append(ProvisionStep(
            name="configure_proxy",
            title="Configure Proxy Settings",
            script=f"""set -euo pipefail
echo '>> Configuring proxy settings...'
{proxy_block}
# Persist proxy in /etc/environment for all users
cat >> /etc/environment <<'PROXYEOF'
{proxy_block}
PROXYEOF
echo 'Proxy configured.'
""",
            timeout=30,
        ))

    # 1. System prerequisites
    steps.append(ProvisionStep(
        name="system_prerequisites",
        title="System Prerequisites (swap, modules, sysctl, firewall)",
        script="""set -euo pipefail
echo '>> Disabling swap...'
swapoff -a
sed -i '/\\bswap\\b/d' /etc/fstab

echo '>> Loading kernel modules...'
cat > /etc/modules-load.d/k8s.conf <<EOF
overlay
br_netfilter
EOF
modprobe overlay
modprobe br_netfilter

echo '>> Setting sysctl parameters...'
cat > /etc/sysctl.d/99-kubernetes.conf <<EOF
net.bridge.bridge-nf-call-iptables  = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward                 = 1
EOF
sysctl --system

echo '>> Disabling SELinux (if present)...'
if command -v setenforce &>/dev/null; then
    setenforce 0 || true
    sed -i 's/^SELINUX=enforcing/SELINUX=permissive/' /etc/selinux/config 2>/dev/null || true
fi

echo '>> Configuring firewalld (if present)...'
if systemctl is-active --quiet firewalld; then
    firewall-cmd --permanent --add-port=6443/tcp
    firewall-cmd --permanent --add-port=2379-2380/tcp
    firewall-cmd --permanent --add-port=10250/tcp
    firewall-cmd --permanent --add-port=10259/tcp
    firewall-cmd --permanent --add-port=10257/tcp
    firewall-cmd --permanent --add-port=30000-32767/tcp
    firewall-cmd --permanent --add-port=8472/udp
    firewall-cmd --reload
fi
echo 'System prerequisites configured.'
""",
        timeout=120,
    ))

    # 2. Custom storage directories (optional)
    dir_cmds = []
    if profile.crio_root != "/var/lib/containers/storage":
        dir_cmds.append(f'mkdir -p "{profile.crio_root}"')
        dir_cmds.append(f'mkdir -p "{profile.crio_runroot}"')
    if profile.kubelet_root != "/var/lib/kubelet":
        dir_cmds.append(f'mkdir -p "{profile.kubelet_root}"')
    if profile.log_root != "/var/log":
        dir_cmds.append(f'mkdir -p "{profile.log_root}/pods"')
        dir_cmds.append(f'mkdir -p "{profile.log_root}/containers"')
    if dir_cmds:
        steps.append(ProvisionStep(
            name="create_custom_dirs",
            title="Create Custom Storage Directories",
            script="set -euo pipefail\necho '>> Creating custom storage directories...'\n"
                   + "\n".join(dir_cmds) + "\necho 'Custom directories created.'",
            timeout=30,
        ))

    # 3. Install CRI-O
    steps.append(ProvisionStep(
        name="install_crio",
        title=f"Install CRI-O {profile.crio_version}",
        script=f"""set -euo pipefail
echo '>> Installing CRI-O {profile.crio_version}...'

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
echo 'CRI-O installed.'
""",
        timeout=300,
    ))

    # 4. Configure CRI-O storage
    steps.append(ProvisionStep(
        name="configure_crio",
        title="Configure CRI-O Storage & Start Service",
        script=f"""set -euo pipefail
echo '>> Configuring CRI-O storage to {profile.crio_root}...'
systemctl daemon-reload
mkdir -p /etc/crio/crio.conf.d
cat > /etc/crio/crio.conf.d/01-storage.conf <<CRIOCONF
[crio]
  root = "{profile.crio_root}"
  runroot = "{profile.crio_runroot}"
  log_dir = "{profile.log_root}/crio/pods"
CRIOCONF

systemctl enable --now crio
echo 'CRI-O configured and running (storage: {profile.crio_root}).'
""",
        timeout=60,
    ))

    # 5. Install kubeadm, kubelet, kubectl
    steps.append(ProvisionStep(
        name="install_k8s",
        title=f"Install Kubernetes {profile.kubernetes_version} Components",
        script=f"""set -euo pipefail
echo '>> Installing Kubernetes {profile.kubernetes_version} components...'

OS="$(. /etc/os-release && echo "$ID")"
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
echo 'Kubernetes components installed.'
""",
        timeout=300,
    ))

    return steps


def get_control_plane_steps(profile: ClusterProfile) -> List[ProvisionStep]:
    """Return the ordered list of discrete steps for control-plane init."""
    cp_nodes = profile.get_control_plane_nodes()
    cp_ip = cp_nodes[0]["ip_address"] if cp_nodes else "CONTROL_PLANE_IP"

    proxy_block = _proxy_env_block(profile)
    audit_log_dir = f"{profile.log_root}/kubernetes"

    kubelet_extra = '    container-runtime-endpoint: "unix:///var/run/crio/crio.sock"'
    if profile.kubelet_root != "/var/lib/kubelet":
        kubelet_extra += f'\n    root-dir: "{profile.kubelet_root}"'

    steps: List[ProvisionStep] = []

    # 0. Proxy on CP (optional)
    if proxy_block:
        steps.append(ProvisionStep(
            name="cp_proxy",
            title="Set Proxy Environment for kubeadm",
            script=f"""set -euo pipefail
echo '>> Setting proxy environment for kubeadm...'
{proxy_block}
echo 'Proxy environment set.'
""",
            timeout=15,
        ))

    # 1. kubeadm init
    steps.append(ProvisionStep(
        name="kubeadm_init",
        title="Run kubeadm init",
        script=f"""set -euo pipefail
echo '>> Preparing kubeadm config...'
mkdir -p "{audit_log_dir}"
cat > /tmp/kubeadm-config.yaml <<EOF
apiVersion: kubeadm.k8s.io/v1beta3
kind: InitConfiguration
localAPIEndpoint:
  advertiseAddress: "{cp_ip}"
  bindPort: 6443
nodeRegistration:
  criSocket: "unix:///var/run/crio/crio.sock"
  kubeletExtraArgs:
{kubelet_extra}
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
    audit-log-path: "{audit_log_dir}/audit.log"
    audit-log-maxage: "30"
    audit-log-maxbackup: "10"
    audit-log-maxsize: "100"
  extraVolumes:
  - name: audit-log
    hostPath: "{audit_log_dir}"
    mountPath: "{audit_log_dir}"
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

echo '>> Running kubeadm init (this may take a few minutes)...'
kubeadm init --config=/tmp/kubeadm-config.yaml --upload-certs | tee /tmp/kubeadm-init.log
echo 'kubeadm init complete.'
""",
        timeout=600,
    ))

    # 2. Configure kubectl
    steps.append(ProvisionStep(
        name="configure_kubectl",
        title="Configure kubectl for root user",
        script="""set -euo pipefail
echo '>> Configuring kubectl...'
mkdir -p /root/.kube
cp /etc/kubernetes/admin.conf /root/.kube/config
chown root:root /root/.kube/config
kubectl get nodes
echo 'kubectl configured.'
""",
        timeout=30,
    ))

    # 3. Install Flannel CNI
    flannel_manifest = profile.flannel_manifest_path or FLANNEL_MANIFEST_URL
    # If the user uploaded a local file we SCP it first; otherwise download URL
    if profile.flannel_manifest_path:
        flannel_apply = (
            "echo '>> Using user-provided Flannel manifest...'\n"
            "kubectl apply -f /tmp/kube-flannel-custom.yml"
        )
    else:
        flannel_apply = f"kubectl apply -f {FLANNEL_MANIFEST_URL}"

    steps.append(ProvisionStep(
        name="install_flannel",
        title="Install Flannel CNI",
        script=f"""set -euo pipefail
echo '>> Installing Flannel CNI...'
{flannel_apply}
echo '>> Waiting for Flannel pods to be ready...'
kubectl -n kube-flannel wait --for=condition=ready pod -l app=flannel --timeout=120s || true
echo 'Flannel CNI installed.'
""",
        timeout=180,
    ))

    # 4. Pod Security Standards
    steps.append(ProvisionStep(
        name="pod_security",
        title=f"Apply Pod Security Standards ({profile.pod_security_standard})",
        script=f"""set -euo pipefail
echo '>> Applying Pod Security Standards ({profile.pod_security_standard})...'
kubectl label namespace default \\
    pod-security.kubernetes.io/enforce={profile.pod_security_standard} \\
    pod-security.kubernetes.io/warn={profile.pod_security_standard} \\
    pod-security.kubernetes.io/audit={profile.pod_security_standard} \\
    --overwrite
echo 'Pod Security Standards applied.'
""",
        timeout=30,
    ))

    # 5. Generate join command
    steps.append(ProvisionStep(
        name="generate_join_cmd",
        title="Generate Worker Join Command",
        script="""set -euo pipefail
echo '>> Generating worker join command...'
kubeadm token create --print-join-command > /tmp/kubeadm-join-command.txt
echo 'Join command:'
cat /tmp/kubeadm-join-command.txt
""",
        timeout=30,
    ))

    return steps


def get_worker_join_steps(join_command: str) -> List[ProvisionStep]:
    """Return the step(s) to join a worker node to the cluster."""
    return [
        ProvisionStep(
            name="join_cluster",
            title="Join Cluster",
            script=f"""set -euo pipefail
echo '>> Joining cluster...'
{join_command} --cri-socket unix:///var/run/crio/crio.sock
echo 'Successfully joined the cluster.'
""",
            timeout=300,
        ),
    ]


def get_best_practices_steps() -> List[ProvisionStep]:
    """Return the ordered list of best-practices hardening steps."""
    return [
        ProvisionStep(
            name="network_policy",
            title="Apply Default-Deny NetworkPolicy",
            script="""set -euo pipefail
echo '>> Creating default-deny network policy for default namespace...'
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
echo 'NetworkPolicy applied.'
""",
            timeout=30,
        ),
        ProvisionStep(
            name="resource_quota",
            title="Set Resource Quotas",
            script="""set -euo pipefail
echo '>> Setting resource quotas...'
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
echo 'ResourceQuota applied.'
""",
            timeout=30,
        ),
        ProvisionStep(
            name="limit_range",
            title="Set Limit Ranges",
            script="""set -euo pipefail
echo '>> Setting limit ranges...'
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
echo 'LimitRange applied.'
""",
            timeout=30,
        ),
        ProvisionStep(
            name="rbac_reader",
            title="Create Read-Only ClusterRole",
            script="""set -euo pipefail
echo '>> Creating read-only ClusterRole...'
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
echo 'ClusterRole cluster-reader created.'
""",
            timeout=30,
        ),
        ProvisionStep(
            name="audit_log_dir",
            title="Ensure Audit Log Directory",
            script="""set -euo pipefail
echo '>> Ensuring audit log directory exists...'
mkdir -p /var/log/kubernetes
echo 'Audit log directory ready.'
""",
            timeout=15,
            fatal=False,
        ),
    ]


def execute_provision_steps(
    node: dict,
    steps: List[ProvisionStep],
) -> List[tuple]:
    """Execute a list of provision steps on a node.

    Returns a list of (ProvisionStep, SSHResult) tuples.
    Stops at the first fatal failure.
    """
    results: List[tuple] = []
    for step in steps:
        result = _run_step(node, step)
        results.append((step, result))
        if not result.success and step.fatal:
            break
    return results


# ── Legacy wrapper functions (kept for backward compatibility) ────────────


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
    """Ask the LLM for cluster setup advice based on the profile.

    Returns a graceful message when the LLM is not configured.
    """
    from modules.llm_client import query_llm  # lazy import — LLM is optional

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
- CRI-O Storage Root: {profile.crio_root}
- Kubelet Data Dir: {profile.kubelet_root}
- Log Root: {profile.log_root}
- HTTP Proxy: {profile.http_proxy or 'none'}
- HTTPS Proxy: {profile.https_proxy or 'none'}
- Alternate HTTP Proxy: {profile.http_proxy_alt or 'none'}
- Alternate HTTPS Proxy: {profile.https_proxy_alt or 'none'}

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


def run_kubectl(profile: ClusterProfile, command: str, timeout: int = 30) -> SSHResult:
    """Run a kubectl (or helm) command against the cluster.

    For imported clusters (with kubeconfig), commands run locally.
    For provisioned clusters, commands run via SSH on the control-plane node.

    If `command` starts with 'helm ', it is treated as a helm command and
    the KUBECONFIG env var is set instead of prefixing with 'kubectl'.
    """
    is_helm = command.strip().startswith("helm ")

    if profile.kubeconfig_content:
        # Write kubeconfig to a file and run locally
        kubeconfig_path = os.path.join(
            config.DATA_DIR, "kubeconfigs", f"{profile.name}.kubeconfig"
        )
        os.makedirs(os.path.dirname(kubeconfig_path), exist_ok=True)
        with open(kubeconfig_path, "w") as f:
            f.write(profile.kubeconfig_content)

        if is_helm:
            full_cmd = f"KUBECONFIG={kubeconfig_path} {command}"
        else:
            full_cmd = f"kubectl --kubeconfig={kubeconfig_path} {command}"
        try:
            proc = subprocess.run(
                full_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return SSHResult(
                hostname="local (kubeconfig)",
                command=full_cmd,
                return_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                success=proc.returncode == 0,
            )
        except subprocess.TimeoutExpired:
            return SSHResult(
                hostname="local (kubeconfig)",
                command=full_cmd,
                return_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout}s",
                success=False,
            )
        except Exception as exc:
            return SSHResult(
                hostname="local (kubeconfig)",
                command=full_cmd,
                return_code=-1,
                stdout="",
                stderr=str(exc),
                success=False,
            )
    else:
        # Provisioned cluster — SSH to control-plane
        cp_nodes = profile.get_control_plane_nodes()
        if not cp_nodes:
            return SSHResult(
                hostname="N/A",
                command=command,
                return_code=-1,
                stdout="",
                stderr="No control-plane node defined",
                success=False,
            )
        cp = cp_nodes[0]
        remote_cmd = command if is_helm else f"kubectl {command}"
        return run_ssh_command(
            ip_address=cp["ip_address"],
            command=remote_cmd,
            ssh_user=cp.get("ssh_user", "root"),
            ssh_port=cp.get("ssh_port", 22),
            ssh_key_path=cp.get("ssh_key_path", "~/.ssh/id_rsa"),
            timeout=timeout,
        )


def upload_flannel_manifest_to_node(node: dict, local_path: str) -> SSHResult:
    """SCP a user-provided Flannel manifest to a node as /tmp/kube-flannel-custom.yml."""
    scp_cmd = [
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-P", str(node.get("ssh_port", 22)),
        "-i", node.get("ssh_key_path", "~/.ssh/id_rsa"),
        local_path,
        f"{node.get('ssh_user', 'root')}@{node['ip_address']}:/tmp/kube-flannel-custom.yml",
    ]
    try:
        proc = subprocess.run(
            scp_cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return SSHResult(
            hostname=node["ip_address"],
            command="scp flannel manifest",
            return_code=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            success=proc.returncode == 0,
        )
    except subprocess.TimeoutExpired:
        return SSHResult(
            hostname=node["ip_address"],
            command="scp flannel manifest",
            return_code=-1,
            stdout="",
            stderr="SCP timed out after 60 seconds",
            success=False,
        )
    except Exception as exc:
        return SSHResult(
            hostname=node["ip_address"],
            command="scp flannel manifest",
            return_code=-1,
            stdout="",
            stderr=str(exc),
            success=False,
        )
