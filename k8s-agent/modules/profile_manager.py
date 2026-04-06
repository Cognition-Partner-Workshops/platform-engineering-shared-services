"""Cluster Profile Manager — CRUD operations for K8s cluster profiles."""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

import config


@dataclass
class NodeInfo:
    """Represents a node in the cluster."""

    hostname: str
    ip_address: str
    role: str  # "control-plane" or "worker"
    ssh_user: str = "root"
    ssh_port: int = 22
    ssh_key_path: str = "~/.ssh/id_rsa"


@dataclass
class ClusterProfile:
    """Represents a complete cluster profile configuration."""

    name: str
    description: str = ""
    kubernetes_version: str = "1.30"
    crio_version: str = "1.30"
    cni_plugin: str = "flannel"
    pod_cidr: str = "10.244.0.0/16"
    service_cidr: str = "10.96.0.0/12"
    dns_domain: str = "cluster.local"
    nodes: list[dict] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    status: str = "draft"  # draft, provisioning, active, error
    kubeconfig_path: str = ""
    monitoring_enabled: bool = False
    pod_security_standard: str = "restricted"  # privileged, baseline, restricted
    # CRI-O storage paths (override defaults in /var/lib)
    crio_root: str = "/var/lib/containers/storage"  # container storage root
    crio_runroot: str = "/run/containers/storage"  # runtime root
    kubelet_root: str = "/var/lib/kubelet"  # kubelet data dir
    log_root: str = "/var/log"  # base log directory
    # Proxy settings for master node
    http_proxy: str = ""
    https_proxy: str = ""
    no_proxy: str = ""
    http_proxy_alt: str = ""  # alternate proxy
    https_proxy_alt: str = ""  # alternate proxy
    # Offline manifest paths — user-provided files for air-gapped environments
    flannel_manifest_path: str = ""  # local path to kube-flannel.yml
    prometheus_manifest_path: str = ""  # local path to prometheus manifest
    # Kubeconfig for existing clusters (imported, not provisioned)
    kubeconfig_content: str = ""  # raw kubeconfig YAML content
    cluster_source: str = "provisioned"  # "provisioned" or "imported"

    def get_control_plane_nodes(self) -> list[dict]:
        return [n for n in self.nodes if n.get("role") == "control-plane"]

    def get_worker_nodes(self) -> list[dict]:
        return [n for n in self.nodes if n.get("role") == "worker"]


def _profile_path(name: str) -> str:
    """Return the file path for a given profile name."""
    safe_name = name.replace(" ", "_").replace("/", "_").lower()
    return os.path.join(config.PROFILES_DIR, f"{safe_name}.json")


def save_profile(profile: ClusterProfile) -> str:
    """Save a cluster profile to disk.

    Returns the file path where the profile was saved.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if not profile.created_at:
        profile.created_at = now
    profile.updated_at = now

    path = _profile_path(profile.name)
    with open(path, "w") as f:
        json.dump(asdict(profile), f, indent=2)
    return path


def load_profile(name: str) -> Optional[ClusterProfile]:
    """Load a cluster profile from disk by name."""
    path = _profile_path(name)
    if not os.path.exists(path):
        return None
    with open(path, "r") as f:
        data = json.load(f)
    return ClusterProfile(**data)


def list_profiles() -> list[ClusterProfile]:
    """List all saved cluster profiles."""
    profiles = []
    if not os.path.exists(config.PROFILES_DIR):
        return profiles
    for filename in sorted(os.listdir(config.PROFILES_DIR)):
        if filename.endswith(".json"):
            filepath = os.path.join(config.PROFILES_DIR, filename)
            try:
                with open(filepath, "r") as f:
                    data = json.load(f)
                profiles.append(ClusterProfile(**data))
            except (json.JSONDecodeError, TypeError):
                continue
    return profiles


def delete_profile(name: str) -> bool:
    """Delete a cluster profile by name.

    Returns True if the profile was deleted, False if it didn't exist.
    """
    path = _profile_path(name)
    if os.path.exists(path):
        os.remove(path)
        return True
    return False


def update_profile_status(name: str, status: str) -> bool:
    """Update the status field of an existing profile."""
    profile = load_profile(name)
    if profile is None:
        return False
    profile.status = status
    save_profile(profile)
    return True
