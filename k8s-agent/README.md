# K8s Agent — On-Prem Kubernetes Cluster Management

A Streamlit-based UI for managing on-premises Kubernetes clusters with CRI-O container runtime and Flannel CNI.

## Features

1. **Profile Manager** — Create and manage profiles for multiple clusters with node definitions (control-plane / worker), SSH credentials, and K8s configuration.

2. **Cluster Creation** — SSH into nodes and provision a full Kubernetes cluster:
   - Installs CRI-O container runtime
   - Installs kubeadm, kubelet, kubectl
   - Initializes control plane with best-practice kubeadm config
   - Deploys Flannel CNI
   - Joins worker nodes automatically
   - Applies security hardening (NetworkPolicies, RBAC, ResourceQuotas, PodSecurity)

3. **Cluster Debugger** — Run diagnostic commands and get AI-powered analysis:
   - Pre-built checks for nodes, pods, networking, storage, certificates
   - Category-based scanning (Cluster Overview, Networking, Security, etc.)
   - Custom command execution via SSH
   - AI-powered root cause analysis and remediation recommendations

4. **Monitoring Setup** — Deploy Prometheus + Grafana with production-ready configuration:
   - One-click kube-prometheus-stack installation
   - Grafana dashboard imports (cluster overview, node exporter, pods, etcd, API server, etc.)
   - Alerting rules for node health, pod crashes, disk pressure, etcd latency
   - AI-powered monitoring recommendations

5. **Log Analysis** — Collect, parse, and correlate logs across cluster components:
   - System component logs (kubelet, CRI-O, API server, etcd, Flannel, CoreDNS)
   - Pod-level log collection with previous container support
   - Automated error pattern extraction and grouping
   - Cross-source error correlation
   - AI-powered deep log analysis and root cause identification

6. **AI Assistant** — Chat interface for Kubernetes questions powered by your LLM.

## Quick Start

```bash
cd k8s-agent
pip install -r requirements.txt

# Set your LLM API key
export LLM_API_KEY="your-api-key"
# Or use the Infosys AI Gateway key
export INFOSYS_CODER_API_KEY="your-key"

# Run the app
streamlit run app.py
```

## Configuration

Environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `LLM_API_URL` | LLM API endpoint | Infosys AI Gateway |
| `LLM_API_KEY` | LLM API key | Falls back to `INFOSYS_CODER_API_KEY` |
| `LLM_MODEL` | Model name | `gpt-4` |
| `LLM_TEMPERATURE` | Response temperature | `0.3` |
| `LLM_MAX_TOKENS` | Max response tokens | `4096` |

## Architecture

```
k8s-agent/
├── app.py                     # Main Streamlit application
├── config.py                  # Configuration and environment variables
├── requirements.txt           # Python dependencies
├── modules/
│   ├── llm_client.py          # LLM API integration (query + streaming)
│   ├── profile_manager.py     # Cluster profile CRUD operations
│   ├── cluster_creator.py     # SSH-based cluster provisioning
│   ├── cluster_debugger.py    # Diagnostic commands and AI analysis
│   ├── monitoring_setup.py    # Prometheus/Grafana deployment
│   └── log_analyzer.py        # Log collection, parsing, correlation
├── templates/                 # Configuration templates
└── data/profiles/             # Stored cluster profiles (JSON)
```

## Requirements

- Python 3.10+
- SSH access to target nodes (for cluster operations)
- LLM API endpoint (Infosys AI Gateway or compatible OpenAI-style API)
