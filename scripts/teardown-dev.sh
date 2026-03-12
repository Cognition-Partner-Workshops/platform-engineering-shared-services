#!/usr/bin/env bash
################################################################################
# Tear down the dev environment to avoid ongoing AWS charges.
#
# Usage:
#   export AWS_ACCESS_KEY_ID=...
#   export AWS_SECRET_ACCESS_KEY=...
#   ./scripts/teardown-dev.sh
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
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

for RELEASE_NS in "argocd:argocd" "prometheus:monitoring" "cert-manager:cert-manager" "ingress-nginx:ingress-nginx"; do
  RELEASE="${RELEASE_NS%%:*}"
  NS="${RELEASE_NS##*:}"
  echo "  Uninstalling $RELEASE from $NS..."
  helm uninstall "$RELEASE" -n "$NS" 2>/dev/null || echo "  (already removed)"
done
echo "  ✓ Helm releases removed"

echo ""
echo "[2/3] Destroying Terraform infrastructure..."
cd "$REPO_ROOT/terraform/environments/dev"
terraform init -input=false
terraform destroy -auto-approve -input=false
echo "  ✓ Infrastructure destroyed"

echo ""
echo "[3/3] Done. The state backend (S3 + DynamoDB) is preserved."
echo "  To destroy the state backend too, run:"
echo "    cd terraform/bootstrap && terraform destroy -auto-approve"
