# Platform Engineering Shared Services

Shared platform infrastructure used across all workshop demo applications. Provisions and manages the EKS cluster, networking, container registry, ingress, TLS, DNS, observability, GitOps (ArgoCD), and namespace isolation.

This repo is the **platform standard** — application teams use it as context when onboarding new services. Every microservice decomposed from a monolith must conform to the patterns defined here.

## What This Repo Manages

| Component | Tool | Description |
|-----------|------|-------------|
| EKS Cluster | Terraform | AWS EKS cluster with managed node groups |
| VPC/Networking | Terraform | VPC, subnets, security groups, NAT gateways |
| Container Registry | Terraform (ECR) | Per-service ECR repositories with lifecycle policies |
| Namespace Provisioning | Terraform (K8s) | App namespaces with resource quotas and limit ranges |
| DNS | Terraform + ExternalDNS | Route 53 hosted zone and automatic DNS record management |
| Ingress Controller | Helm (ingress-nginx) | NGINX Ingress Controller for L7 routing |
| TLS Certificates | Helm (cert-manager) | Automatic Let's Encrypt TLS certificates |
| Monitoring | Helm (Prometheus + Grafana) | Cluster-wide metrics and dashboards |
| GitOps | Helm (ArgoCD) | Declarative continuous delivery for Kubernetes |
| Network Policies | K8s manifests | Default-deny with explicit allow rules per namespace |

## Project Structure

```
terraform/
├── bootstrap/               # One-time: S3 + DynamoDB for Terraform state
├── modules/
│   ├── eks-cluster/         # EKS cluster, node groups, IRSA roles
│   ├── networking/          # VPC, subnets, security groups, NAT
│   ├── ecr/                 # ECR repositories with lifecycle policies
│   ├── namespaces/          # K8s namespace provisioning with quotas
│   └── dns/                 # Route 53 hosted zone
├── environments/
│   ├── dev/                 # Dev environment (t3.medium, 2 nodes, single NAT)
│   ├── staging/             # Staging environment
│   └── prod/                # Production environment
helm-releases/
├── ingress-nginx/           # NGINX Ingress Controller values
├── cert-manager/            # cert-manager + ClusterIssuer
├── monitoring/
│   ├── prometheus/          # Prometheus stack values
│   └── grafana/             # Grafana dashboards + values
├── external-dns/            # ExternalDNS for Route 53
└── argocd/                  # Argo CD for GitOps deployments
k8s/
├── network-policies/        # Default-deny + allow templates per namespace
└── resource-quotas/         # Standard resource quota templates
scripts/
├── deploy-dev.sh            # End-to-end dev deployment (bootstrap → infra → helm)
└── teardown-dev.sh          # Destroy dev environment to save costs
docs/
├── architecture.md          # Platform architecture overview
└── onboarding.md            # How app teams onboard to the platform
```

## Quick Start

### One-Command Deploy (Dev)

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
./scripts/deploy-dev.sh
```

This bootstraps the state backend, provisions all infrastructure, configures kubectl, and installs all shared Helm releases. Takes ~20 minutes for a fresh cluster.

### Manual Step-by-Step

```bash
# 1. Bootstrap state backend (one-time)
cd terraform/bootstrap
terraform init && terraform apply -auto-approve

# 2. Provision infrastructure
cd terraform/environments/dev
terraform init && terraform apply -auto-approve

# 3. Configure kubectl
aws eks update-kubeconfig --region us-east-1 --name workshop-dev

# 4. Install Helm releases (see deploy-dev.sh for full list)
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -f helm-releases/ingress-nginx/values.yaml \
  -n ingress-nginx --create-namespace
```

### Teardown (Stop AWS Charges)

```bash
./scripts/teardown-dev.sh
```

## App Team Onboarding

Application teams deploy their own Helm charts into dedicated namespaces. They depend on this shared infrastructure for:

1. **EKS cluster** — compute, networking, IAM
2. **ECR** — container image registry with scan-on-push
3. **Ingress controller** — L7 routing via Ingress resources
4. **cert-manager** — automatic TLS via cert-manager annotations
5. **Monitoring** — Prometheus ServiceMonitor resources for metrics
6. **ArgoCD** — GitOps-driven deployments from app IaC repos
7. **DNS** — automatic DNS records via ExternalDNS annotations
8. **Network policies** — default-deny with explicit ingress/egress rules

See [`docs/onboarding.md`](docs/onboarding.md) for detailed instructions.

## Platform vs Service IaC Split

| Concern | Owned By | Repo |
|---------|----------|------|
| EKS cluster, VPC, shared services | Platform team | This repo (`platform-engineering-shared-services`) |
| App Helm charts, Dockerfiles, CI/CD | App team | `app_dotnet-angular-monolith-iac` (or similar) |
| ECR repositories | Platform team | This repo (Terraform ECR module) |
| ArgoCD Application manifests | App team | App IaC repo (references platform ArgoCD) |
| Namespace creation + quotas | Platform team | This repo (Terraform namespace module) |
| Network policies (base) | Platform team | This repo (`k8s/network-policies/`) |
| Network policies (app-specific) | App team | App IaC repo (extends base policies) |

## Workshop Demo Applications Using This Platform

| App Repos | Description |
|-----------|-------------|
| `app_dotnet-angular-monolith` | .NET 8 + Angular monolith (the "before" state) |
| `app_dotnet-angular-monolith-iac` | Service-specific Helm charts, Dockerfiles, ArgoCD manifests |
