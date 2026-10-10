#!/usr/bin/env bash
################################################################################
# Delete everything created by provision.sh (matched by Project=ssm-portfwd-demo
# or read from the local .demo-state file).
#
# Usage:
#   ./teardown.sh
#
# Note: the shared instance profile (EC2-SSM-InstanceProfile) is intentionally
# NOT deleted, since provision.sh only creates it when missing and it may be
# shared. Delete it manually if this demo created it and you no longer need it.
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.demo-state"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-2}}"
PROJECT="ssm-portfwd-demo"

if [ -f "$STATE_FILE" ]; then
  # shellcheck disable=SC1090
  source "$STATE_FILE"
fi
export AWS_DEFAULT_REGION="$REGION"

echo "Tearing down project '$PROJECT' in $REGION ..."

# Resolve resources by tag so teardown works even without a state file.
IIDS=$(aws ec2 describe-instances \
  --filters "Name=tag:Project,Values=$PROJECT" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
if [ -n "${IIDS// /}" ]; then
  echo "  terminating instances: $IIDS"
  # shellcheck disable=SC2086  # intentional word-splitting across multiple IDs
  aws ec2 terminate-instances --instance-ids $IIDS >/dev/null
  # shellcheck disable=SC2086
  aws ec2 wait instance-terminated --instance-ids $IIDS
fi

VPCS=$(aws ec2 describe-vpcs --filters "Name=tag:Project,Values=$PROJECT" \
  --query 'Vpcs[].VpcId' --output text)
for VPC in $VPCS; do
  echo "  cleaning VPC $VPC"
  EPS=$(aws ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$VPC" \
    --query 'VpcEndpoints[].VpcEndpointId' --output text)
  if [ -n "${EPS// /}" ]; then
    echo "    deleting endpoints: $EPS"
    # shellcheck disable=SC2086  # intentional word-splitting across multiple IDs
    aws ec2 delete-vpc-endpoints --vpc-endpoint-ids $EPS >/dev/null
    # endpoints take a moment to detach their ENIs
    sleep 30
  fi

  SGS=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=$VPC" "Name=group-name,Values=$PROJECT-sg" \
    --query 'SecurityGroups[].GroupId' --output text)
  for SG in $SGS; do echo "    deleting SG $SG"; aws ec2 delete-security-group --group-id "$SG" || true; done

  SUBNETS=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" \
    --query 'Subnets[].SubnetId' --output text)
  for S in $SUBNETS; do echo "    deleting subnet $S"; aws ec2 delete-subnet --subnet-id "$S" || true; done

  echo "    deleting VPC $VPC"
  aws ec2 delete-vpc --vpc-id "$VPC" || true
done

if aws ec2 describe-key-pairs --key-names "$PROJECT" >/dev/null 2>&1; then
  echo "  deleting key pair $PROJECT"
  aws ec2 delete-key-pair --key-name "$PROJECT" >/dev/null
fi

rm -f "$STATE_FILE"
echo "Teardown complete."
