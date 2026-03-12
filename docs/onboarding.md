# App Team Onboarding Guide

This document explains how application teams deploy their workloads onto the shared workshop platform. Use this as context when decomposing a monolith into microservices — every new service must follow these patterns.

## Prerequisites

1. **kubectl access** — configured for the workshop EKS cluster
2. **Helm 3.x** — installed locally
3. **AWS CLI** — configured with ECR push permissions
4. **Docker** — for building container images

## Platform Provides (You Get for Free)

| Service | Namespace | What It Does |
|---------|-----------|-------------|
| ingress-nginx | `ingress-nginx` | L7 load balancing via Ingress resources |
| cert-manager | `cert-manager` | Automatic TLS certificates |
| Prometheus + Grafana | `monitoring` | Metrics collection and dashboards |
| ArgoCD | `argocd` | GitOps continuous delivery |
| ExternalDNS | `external-dns` | Automatic DNS record management |

## Onboarding a New Microservice

### 1. Request a Namespace (Platform Team)

Namespaces are provisioned via Terraform in this repo. Add your namespace to the `app_namespaces` local in the appropriate environment file (e.g., `terraform/environments/dev/main.tf`):

```hcl
{
  name        = "my-app-dev"
  environment = "dev"
  team        = "my-team"
}
```

Each namespace comes pre-configured with:
- Resource quotas (CPU/memory limits)
- Limit ranges (default container resource requests)
- Network policies (default-deny with allowed ingress/monitoring)

### 2. Request an ECR Repository (Platform Team)

Add your service image name to the `ecr_repositories` local:

```hcl
ecr_repositories = [
  "workshop/my-new-service",
  # ...existing repos...
]
```

### 3. Build and Push Container Image

```bash
# Authenticate Docker to ECR
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 599083837640.dkr.ecr.us-east-1.amazonaws.com

# Build and push
docker build -t workshop/my-service:v1.0.0 .
docker tag workshop/my-service:v1.0.0 599083837640.dkr.ecr.us-east-1.amazonaws.com/workshop/my-service:v1.0.0
docker push 599083837640.dkr.ecr.us-east-1.amazonaws.com/workshop/my-service:v1.0.0
```

### 4. Create a Helm Chart

Your Helm chart lives in your app's IaC repo (e.g., `app_dotnet-angular-monolith-iac`). It must include:

**Deployment** with proper resource requests/limits:
```yaml
resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 500m
    memory: 256Mi
```

**Service** exposing your application port:
```yaml
apiVersion: v1
kind: Service
metadata:
  name: my-service
spec:
  ports:
    - port: 80
      targetPort: 8080
  selector:
    app: my-service
```

**Ingress** resource using the shared ingress controller:
```yaml
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: my-service
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-staging
spec:
  ingressClassName: nginx
  rules:
    - host: my-service.workshop.local
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: my-service
                port:
                  number: 80
```

**ServiceMonitor** for Prometheus metrics:
```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: my-service
spec:
  selector:
    matchLabels:
      app: my-service
  endpoints:
    - port: http
      path: /metrics
```

**NetworkPolicy** (extends the base default-deny from the platform):
```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: my-service-egress
spec:
  podSelector:
    matchLabels:
      app: my-service
  policyTypes:
    - Egress
  egress:
    - to:
        - podSelector:
            matchLabels:
              app: my-database
      ports:
        - protocol: TCP
          port: 5432
```

### 5. Deploy with Helm (Manual)

```bash
helm upgrade --install my-service charts/my-service \
  -f environments/dev/values.yaml \
  -n my-app-dev
```

### 6. Deploy with ArgoCD (GitOps)

Create an ArgoCD `Application` manifest in your IaC repo:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: my-service
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/Cognition-Partner-Workshops/app_dotnet-angular-monolith-iac
    targetRevision: main
    path: charts/my-service
    helm:
      valueFiles:
        - ../../environments/dev/values.yaml
  destination:
    server: https://kubernetes.default.svc
    namespace: decomposition-dev
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
```

Apply it:
```bash
kubectl apply -f argocd/my-service.yaml
```

### 7. Verify

```bash
# Check pod status
kubectl get pods -n my-app-dev

# Check ingress
kubectl get ingress -n my-app-dev

# Check ArgoCD sync status
kubectl get applications -n argocd

# View in ArgoCD UI (port-forward)
kubectl port-forward svc/argocd-server -n argocd 8080:443
```
