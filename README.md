# Platform Engineering Shared Services

Shared platform infrastructure used across all workshop demo applications. Provisions and manages the EKS cluster, networking, container registry, ingress, TLS, DNS, observability, GitOps (ArgoCD), and namespace isolation.

This repo is the **platform standard** — application teams use it as context when onboarding new services. Every microservice decomposed from a monolith must conform to the patterns defined here.

## What This Repo Manages

| Component | Tool | Description |
|-----------|------|-------------|
| EKS Cluster | AWS CDK | AWS EKS cluster with managed node groups |
| VPC/Networking | AWS CDK | VPC, subnets, security groups, NAT gateways |
| Container Registry | AWS CDK (ECR) | Per-service ECR repositories with lifecycle policies |
| Namespace Provisioning | AWS CDK (K8s) | App namespaces with resource quotas and limit ranges |
| DNS | AWS CDK + ExternalDNS | Route 53 hosted zone and automatic DNS record management |
| Ingress Controller | Helm (ingress-nginx) | NGINX Ingress Controller for L7 routing |
| TLS Certificates | Helm (cert-manager) | Automatic Let's Encrypt TLS certificates |
| Monitoring | Helm (Prometheus + Grafana) | Cluster-wide metrics and dashboards |
| GitOps | Helm (ArgoCD) | Declarative continuous delivery for Kubernetes |
| Shared Cache | AWS CDK (ElastiCache) + Helm (Redis) | Single-source-of-truth caching layer for microservices |
| Network Policies | K8s manifests | Default-deny with explicit allow rules per namespace |

> **Note:** All CDK resources use `RemovalPolicy.DESTROY` — stack deletion removes everything with no orphaned resources.

## Project Structure

```
cdk/
├── bin/cdk.ts               # CDK app entry point (dev/staging/prod stacks)
├── lib/
│   ├── platform-stack.ts    # Main stack orchestrating all constructs
│   └── constructs/
│       ├── networking.ts    # VPC, subnets, NAT gateways, ELB tags
│       ├── eks-cluster.ts   # EKS cluster, managed node groups
│       ├── ecr-repositories.ts  # ECR repos with lifecycle policies
│       ├── dns-zone.ts      # Route 53 hosted zone
│       ├── k8s-namespaces.ts    # Namespaces with quotas and limit ranges
│       ├── elasticache-redis.ts # ElastiCache Redis shared cache
│       └── index.ts         # Barrel exports
├── package.json
├── tsconfig.json
└── cdk.json                 # CDK context and feature flags
terraform/                   # (legacy — being replaced by cdk/)
helm-releases/
├── ingress-nginx/           # NGINX Ingress Controller values
├── cert-manager/            # cert-manager + ClusterIssuer
├── monitoring/
│   ├── prometheus/          # Prometheus stack values
│   └── grafana/             # Grafana dashboards + values
├── external-dns/            # ExternalDNS for Route 53
├── argocd/                  # Argo CD for GitOps deployments
└── redis/                   # In-cluster Redis cache (dev/staging)
k8s/
├── network-policies/        # Default-deny + allow templates per namespace
└── resource-quotas/         # Standard resource quota templates
scripts/
├── deploy-dev.sh            # End-to-end dev deployment (bootstrap → infra → helm)
└── teardown-dev.sh          # Destroy dev environment to save costs
docs/
├── architecture.md          # Platform architecture overview
├── cache-strategy.md        # Cache strategy for single source of truth
└── onboarding.md            # How app teams onboard to the platform
```

## Quick Start

### One-Command Deploy (Dev)

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
./scripts/deploy-dev.sh
```

This bootstraps CDK, deploys the `WorkshopPlatformDev` stack, configures kubectl, and installs all shared Helm releases. Takes ~20 minutes for a fresh cluster.

### Manual Step-by-Step

```bash
# 1. Bootstrap CDK (one-time)
cd cdk && npm install
npx cdk bootstrap

# 2. Deploy the dev stack
npx cdk deploy WorkshopPlatformDev --require-approval never

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
8. **Shared cache** — Redis (ElastiCache + in-cluster) with single-source-of-truth patterns
9. **Network policies** — default-deny with explicit ingress/egress rules

See [`docs/onboarding.md`](docs/onboarding.md) for detailed instructions.

## Platform vs Service IaC Split

| Concern | Owned By | Repo |
|---------|----------|------|
| EKS cluster, VPC, shared services | Platform team | This repo (`platform-engineering-shared-services`) |
| App Helm charts, Dockerfiles, CI/CD | App team | `app_dotnet-angular-monolith-iac` (or similar) |
| ECR repositories | Platform team | This repo (CDK EcrRepositories construct) |
| ArgoCD Application manifests | App team | App IaC repo (references platform ArgoCD) |
| Namespace creation + quotas | Platform team | This repo (CDK K8sNamespaces construct) |
| Network policies (base) | Platform team | This repo (`k8s/network-policies/`) |
| Network policies (app-specific) | App team | App IaC repo (extends base policies) |

## Workshop Demo Applications Using This Platform

| App Repos | Description |
|-----------|-------------|
| `app_dotnet-angular-monolith` | .NET 8 + Angular monolith (the "before" state) |
| `app_dotnet-angular-monolith-iac` | Service-specific Helm charts, Dockerfiles, ArgoCD manifests |
