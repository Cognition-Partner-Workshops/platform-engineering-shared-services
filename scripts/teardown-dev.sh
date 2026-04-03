#!/usr/bin/env bash
################################################################################
# Tear down the dev environment to avoid ongoing AWS charges.
# All CDK resources use RemovalPolicy.DESTROY — stack deletion removes
# everything including ECR images, VPC, and the EKS cluster.
#
# Usage:
#   export AWS_ACCESS_KEY_ID=...
#   export AWS_SECRET_ACCESS_KEY=...
#   ./scripts/teardown-dev.sh
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CDK_DIR="$REPO_ROOT/cdk"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="workshop-dev"

echo "=========================================="
echo "  Workshop Platform — Dev Teardown"
echo "=========================================="
echo ""
echo "WARNING: This will destroy all dev infrastructure including the EKS cluster,"
echo "VPC, ECR repositories, and all workloads running on the cluster."
echo ""
read -p "Type 'destroy' to confirm: " CONFIRM
if [ "$CONFIRM" != "destroy" ]; then
  echo "Aborted."
  exit 1
fi

# Configure kubectl (may fail if cluster is already gone)
echo ""
echo "[1/3] Removing Helm releases..."
aws eks update-kubeconfig --region "$REGION" --name "$CLUSTER_NAME" --alias "$CLUSTER_NAME" 2>/dev/null || true

for RELEASE_NS in "redis:redis" "argocd:argocd" "prometheus:monitoring" "cert-manager:cert-manager" "ingress-nginx:ingress-nginx"; do
  RELEASE="${RELEASE_NS%%:*}"
  NS="${RELEASE_NS##*:}"
  echo "  Uninstalling $RELEASE from $NS..."
  helm uninstall "$RELEASE" -n "$NS" 2>/dev/null || echo "  (already removed)"
done
echo "  ✓ Helm releases removed"

echo ""
echo "[2/3] Destroying CDK stack (all resources use RemovalPolicy.DESTROY)..."
cd "$CDK_DIR"
npm install
npx cdk destroy WorkshopPlatformDev --force
echo "  ✓ Infrastructure destroyed"

echo ""
echo "[3/3] Done. The CDK bootstrap stack is preserved."
echo "  To destroy the bootstrap stack too, run:"
echo "    npx cdk destroy CDKToolkit --force"
