#!/usr/bin/env bash
################################################################################
# Deploy the dev environment end-to-end:
#   1. Bootstrap Terraform state backend (S3 + DynamoDB) — idempotent
#   2. Provision core infrastructure (VPC, EKS, ECR, namespaces)
#   3. Configure kubectl
#   4. Install shared Helm releases (ingress-nginx, cert-manager, monitoring, ArgoCD)
#   5. Apply network policies to app namespaces
#
# Prerequisites:
#   - AWS CLI configured with appropriate credentials
#   - terraform >= 1.5
#   - helm >= 3.x
#   - kubectl
#
# Usage:
#   export AWS_ACCESS_KEY_ID=...
#   export AWS_SECRET_ACCESS_KEY=...
#   ./scripts/deploy-dev.sh
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="workshop-dev"

echo "=========================================="
echo "  Workshop Platform — Dev Deployment"
echo "=========================================="

################################################################################
# Step 1: Bootstrap state backend
################################################################################
echo ""
echo "[1/5] Bootstrapping Terraform state backend..."
cd "$REPO_ROOT/terraform/bootstrap"
terraform init -input=false
terraform apply -auto-approve -input=false
echo "  ✓ State backend ready"

################################################################################
# Step 2: Provision core infrastructure
################################################################################
echo ""
echo "[2/5] Provisioning core infrastructure (VPC, EKS, ECR, namespaces)..."
echo "  This will take 15-20 minutes for a new EKS cluster."
cd "$REPO_ROOT/terraform/environments/dev"
terraform init -input=false
terraform apply -auto-approve -input=false
echo "  ✓ Core infrastructure provisioned"

################################################################################
# Step 3: Configure kubectl
################################################################################
echo ""
echo "[3/5] Configuring kubectl for EKS cluster..."
aws eks update-kubeconfig \
  --region "$REGION" \
  --name "$CLUSTER_NAME" \
  --alias "$CLUSTER_NAME"
echo "  ✓ kubectl configured"
kubectl cluster-info
kubectl get nodes

################################################################################
# Step 4: Install shared Helm releases
################################################################################
echo ""
echo "[4/5] Installing shared Helm releases..."

# Add Helm repos
helm repo add ingress-nginx https://kubernetes.github.io/ingress-nginx 2>/dev/null || true
helm repo add jetstack https://charts.jetstack.io 2>/dev/null || true
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts 2>/dev/null || true
helm repo add argo https://argoproj.github.io/argo-helm 2>/dev/null || true
helm repo update

# Ingress NGINX
echo "  Installing ingress-nginx..."
helm upgrade --install ingress-nginx ingress-nginx/ingress-nginx \
  -f "$REPO_ROOT/helm-releases/ingress-nginx/values.yaml" \
  -n ingress-nginx --create-namespace --wait --timeout 5m

# cert-manager
echo "  Installing cert-manager..."
helm upgrade --install cert-manager jetstack/cert-manager \
  -f "$REPO_ROOT/helm-releases/cert-manager/values.yaml" \
  -n cert-manager --create-namespace --wait --timeout 5m

# Apply ClusterIssuers
echo "  Applying ClusterIssuers..."
kubectl apply -f "$REPO_ROOT/helm-releases/cert-manager/cluster-issuer.yaml"

# Prometheus + Grafana
echo "  Installing kube-prometheus-stack..."
helm upgrade --install prometheus prometheus-community/kube-prometheus-stack \
  -f "$REPO_ROOT/helm-releases/monitoring/prometheus/values.yaml" \
  -f "$REPO_ROOT/helm-releases/monitoring/grafana/values.yaml" \
  -n monitoring --create-namespace --wait --timeout 5m

# Argo CD
echo "  Installing Argo CD..."
helm upgrade --install argocd argo/argo-cd \
  -f "$REPO_ROOT/helm-releases/argocd/values.yaml" \
  -n argocd --create-namespace --wait --timeout 5m

echo "  ✓ All Helm releases installed"

################################################################################
# Step 5: Apply network policies to app namespaces
################################################################################
echo ""
echo "[5/5] Applying network policies to app namespaces..."
for NS in decomposition-dev decomposition-staging; do
  echo "  Applying to namespace: $NS"
  kubectl apply -f "$REPO_ROOT/k8s/network-policies/default-deny.yaml" -n "$NS" 2>/dev/null || \
    echo "  (namespace $NS may not exist yet — skipping)"
done
echo "  ✓ Network policies applied"

################################################################################
# Summary
################################################################################
echo ""
echo "=========================================="
echo "  Deployment Complete!"
echo "=========================================="
echo ""
echo "Cluster:        $CLUSTER_NAME"
echo "Region:         $REGION"
echo "kubectl config: $(kubectl config current-context)"
echo ""
echo "Shared services:"
kubectl get pods -A --no-headers | awk '{print "  "$1"/"$2" ("$3")"}' | head -20
echo ""
echo "ECR Repositories:"
cd "$REPO_ROOT/terraform/environments/dev"
terraform output -json ecr_repository_urls 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "  (run 'terraform output ecr_repository_urls' manually)"
echo ""
echo "ArgoCD initial admin password:"
echo "  kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo"
echo ""
echo "Next steps:"
echo "  1. Push app container images to ECR"
echo "  2. Deploy app Helm charts via ArgoCD or helm install"
echo "  3. See docs/onboarding.md for app team instructions"
