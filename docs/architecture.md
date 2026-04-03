# Platform Architecture

## Overview

The workshop platform runs on AWS EKS with shared services for ingress, TLS, DNS, monitoring, and GitOps. Application teams deploy their own workloads into dedicated namespaces provisioned by the platform team.

This architecture supports the **monolith-to-microservices decomposition** workflow: the platform defines the standard, and each decomposed service conforms to it via its own Helm chart, Dockerfile, and ArgoCD Application manifest.

```
┌──────────────────────────────────────────────────────────────────┐
│                     AWS Account (599083837640)                     │
│                                                                    │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │               VPC (10.x.0.0/16)                           │    │
│  │                                                            │    │
│  │  ┌────────────────────────────────────────────────────┐   │    │
│  │  │           EKS Cluster (workshop-dev)                │   │    │
│  │  │                                                      │   │    │
│  │  │  ┌─────────────── Shared Services ───────────────┐  │   │    │
│  │  │  │                                                 │  │   │    │
│  │  │  │  ┌──────────┐  ┌──────────┐  ┌──────────┐    │  │   │    │
│  │  │  │  │ ingress- │  │  cert-   │  │  argocd  │    │  │   │    │
│  │  │  │  │  nginx   │  │ manager  │  │  (GitOps)│    │  │   │    │
│  │  │  │  └──────────┘  └──────────┘  └──────────┘    │  │   │    │
│  │  │  │  ┌──────────┐  ┌──────────┐                   │  │   │    │
│  │  │  │  │prometheus│  │ external │                   │  │   │    │
│  │  │  │  │ +grafana │  │   dns    │                   │  │   │    │
│  │  │  │  └──────────┘  └──────────┘                   │  │   │    │
│  │  │  └─────────────────────────────────────────────────┘  │   │    │
│  │  │                                                      │   │    │
│  │  │  ┌─────────── App Namespaces (isolated) ───────────┐ │   │    │
│  │  │  │                                                   │ │   │    │
│  │  │  │  decomposition-dev     decomposition-staging     │ │   │    │
│  │  │  │  ┌─────────────────┐  ┌─────────────────┐       │ │   │    │
│  │  │  │  │ order-service   │  │ order-service   │       │ │   │    │
│  │  │  │  │ inventory-svc   │  │ inventory-svc   │       │ │   │    │
│  │  │  │  │ customer-svc    │  │ customer-svc    │       │ │   │    │
│  │  │  │  │ product-svc     │  │ product-svc     │       │ │   │    │
│  │  │  │  │ api-gateway     │  │ api-gateway     │       │ │   │    │
│  │  │  │  │ web-frontend    │  │ web-frontend    │       │ │   │    │
│  │  │  │  └─────────────────┘  └─────────────────┘       │ │   │    │
│  │  │  │  [ResourceQuota] [LimitRange] [NetworkPolicy]   │ │   │    │
│  │  │  └─────────────────────────────────────────────────┘ │   │    │
│  │  └────────────────────────────────────────────────────┘   │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                    │
│  ┌──────────┐  ┌──────────┐  ┌──────────────┐                   │
│  │ Route 53 │  │   ECR    │  │ S3 (TF state)│                   │
│  └──────────┘  └──────────┘  └──────────────┘                   │
└──────────────────────────────────────────────────────────────────┘
```

## CDK Constructs

All infrastructure is defined in AWS CDK (TypeScript) under `cdk/`. Every resource uses `RemovalPolicy.DESTROY` so stack deletion leaves nothing behind.

| Construct | Purpose |
|-----------|---------|
| `Networking` | VPC with public/private subnets, NAT gateways, ELB subnet tags |
| `EksCluster` | EKS cluster with managed node groups, KubectlV31Layer, cluster admin role |
| `EcrRepositories` | ECR repositories with lifecycle policies, scan-on-push, `emptyOnDelete` |
| `DnsZone` | Route 53 public hosted zone (optional) |
| `K8sNamespaces` | Kubernetes namespaces with resource quotas and limit ranges |
| `ElastiCacheRedis` | Shared Redis cache cluster (single source of truth caching layer) |

### CDK Stacks

| Stack | Environment | Description |
|-------|-------------|-------------|
| `WorkshopPlatformDev` | dev | Development environment (2 AZs, 1 NAT, t3.medium nodes) |
| `WorkshopPlatformStaging` | staging | Staging environment (2 AZs, 1 NAT, t3.medium nodes) |
| `WorkshopPlatformProd` | prod | Production environment (3 AZs, 2 NATs, t3.large nodes) |

## Helm Releases

| Release | Namespace | Purpose |
|---------|-----------|---------|
| `ingress-nginx` | `ingress-nginx` | L7 load balancing and routing |
| `cert-manager` | `cert-manager` | Automatic TLS certificate management |
| `kube-prometheus-stack` | `monitoring` | Metrics, alerting, dashboards |
| `external-dns` | `external-dns` | Automatic DNS record management |
| `argo-cd` | `argocd` | GitOps continuous delivery |
| `redis` | `redis` | Shared in-cluster Redis cache (single source of truth) |

## Kubernetes Policies

| Resource | Applied To | Purpose |
|----------|-----------|---------|
| NetworkPolicy (default-deny) | All app namespaces | Deny all traffic by default |
| NetworkPolicy (allow-dns) | All app namespaces | Allow DNS resolution |
| NetworkPolicy (allow-ingress) | All app namespaces | Allow traffic from ingress-nginx |
| NetworkPolicy (allow-prometheus) | All app namespaces | Allow Prometheus metric scraping |
| NetworkPolicy (allow-redis) | All app namespaces | Allow egress to shared Redis cache |
| NetworkPolicy (allow-elasticache) | All app namespaces | Allow egress to ElastiCache on port 6379 |
| ResourceQuota | All app namespaces | Cap total CPU/memory/pods per namespace |
| LimitRange | All app namespaces | Set default container resource requests |

## Deployment Flow (Monolith to Microservices)

```
1. Platform team deploys shared services (this repo)
   └── cdk deploy + helm install

2. Monolith app exists in app_dotnet-angular-monolith repo
   └── Single deployable .NET 8 + Angular app

3. Developer decomposes a service from the monolith
   └── Uses platform-engineering-shared-services as context
   └── Creates new service in app_dotnet-angular-monolith-iac

4. New service conforms to platform standard:
   ├── Dockerfile → builds image → pushes to ECR
   ├── Helm chart → deploys to app namespace
   ├── ServiceMonitor → scraped by Prometheus
   ├── Ingress → routed by ingress-nginx
   ├── NetworkPolicy → extends default-deny
   └── ArgoCD Application → GitOps sync from IaC repo
```
