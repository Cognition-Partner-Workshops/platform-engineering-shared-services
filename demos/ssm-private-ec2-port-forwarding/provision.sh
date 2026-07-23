#!/usr/bin/env bash
################################################################################
# Provision a fully private EC2 instance (NO public IP, NO IGW, NO NAT) that is
# reachable only through the AWS Systems Manager (SSM) agent's outbound reverse
# tunnel via interface VPC endpoints.
#
# What it creates (all tagged Project=ssm-portfwd-demo):
#   - VPC 10.0.0.0/16 (DNS support + hostnames enabled)
#   - One private subnet 10.0.1.0/24 (auto-assign public IP disabled)
#   - A security group with NO inbound SSH; only 443 from within the VPC so the
#     instance can reach the interface endpoints
#   - Interface VPC endpoints for ssm, ssmmessages, ec2messages (private DNS on)
#   - An EC2 instance (Amazon Linux 2023) with an SSM instance profile and no
#     public IP address
#   - An imported EC2 key pair (public half of a locally generated key) so we
#     can later SSH over the SSM port-forward tunnel
#
# The instance has no route to the internet at all (the route table only has the
# local route). It registers with SSM purely over the interface endpoints, which
# is exactly what makes the "reverse tunnel" possible.
#
# Prerequisites:
#   - AWS CLI v2 configured with credentials (export AWS_ACCESS_KEY_ID / _SECRET)
#   - IAM permissions for EC2, VPC endpoints, SSM, and (optionally) IAM to create
#     the instance profile if it does not already exist
#
# Usage:
#   export AWS_ACCESS_KEY_ID=...
#   export AWS_SECRET_ACCESS_KEY=...
#   export AWS_REGION=us-east-2            # optional, defaults to us-east-2
#   ./provision.sh
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.demo-state"

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-2}}"
export AWS_DEFAULT_REGION="$REGION"

PROJECT="ssm-portfwd-demo"
INSTANCE_PROFILE="${INSTANCE_PROFILE:-EC2-SSM-InstanceProfile}"
SSM_ROLE_NAME="${SSM_ROLE_NAME:-EC2-SSM-Role}"
KEY_NAME="$PROJECT"
KEY_PATH="${KEY_PATH:-$HOME/.ssh/$PROJECT}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.micro}"
AZ="${AZ:-${REGION}a}"

echo "=========================================="
echo "  Private EC2 + SSM reverse tunnel — provision"
echo "  region=$REGION  az=$AZ"
echo "=========================================="

tag_spec() { # $1 = ResourceType, $2 = Name
  echo "ResourceType=$1,Tags=[{Key=Name,Value=$2},{Key=Project,Value=$PROJECT}]"
}

################################################################################
# Step 0: Make sure an SSM instance profile exists (create it if missing)
################################################################################
echo ""
echo "[0/6] Ensuring instance profile '$INSTANCE_PROFILE' exists..."
if ! aws iam get-instance-profile --instance-profile-name "$INSTANCE_PROFILE" >/dev/null 2>&1; then
  echo "  creating role '$SSM_ROLE_NAME' + instance profile '$INSTANCE_PROFILE'"
  aws iam create-role --role-name "$SSM_ROLE_NAME" \
    --assume-role-policy-document '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$SSM_ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  aws iam create-instance-profile --instance-profile-name "$INSTANCE_PROFILE" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$INSTANCE_PROFILE" --role-name "$SSM_ROLE_NAME"
  echo "  waiting for instance profile to propagate..."
  sleep 15
else
  echo "  found existing instance profile."
fi

################################################################################
# Step 1: Network — VPC + private subnet (no public IP, no IGW, no NAT)
################################################################################
echo ""
echo "[1/6] Creating VPC and private subnet..."
VPC=$(aws ec2 create-vpc --cidr-block 10.0.0.0/16 \
  --tag-specifications "$(tag_spec vpc $PROJECT)" --query Vpc.VpcId --output text)
aws ec2 modify-vpc-attribute --vpc-id "$VPC" --enable-dns-support
aws ec2 modify-vpc-attribute --vpc-id "$VPC" --enable-dns-hostnames

SUBNET=$(aws ec2 create-subnet --vpc-id "$VPC" --cidr-block 10.0.1.0/24 \
  --availability-zone "$AZ" \
  --tag-specifications "$(tag_spec subnet $PROJECT-private)" --query Subnet.SubnetId --output text)
aws ec2 modify-subnet-attribute --subnet-id "$SUBNET" --no-map-public-ip-on-launch
echo "  VPC=$VPC  SUBNET=$SUBNET"

################################################################################
# Step 2: Security group — zero inbound SSH; only 443 from the VPC
################################################################################
echo ""
echo "[2/6] Creating security group (no inbound SSH; 443 from VPC only)..."
SG=$(aws ec2 create-security-group --group-name "$PROJECT-sg" \
  --description "SSM demo: no inbound SSH; 443 for interface VPC endpoints" \
  --vpc-id "$VPC" --tag-specifications "$(tag_spec security-group $PROJECT-sg)" \
  --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" \
  --protocol tcp --port 443 --cidr 10.0.0.0/16 >/dev/null
echo "  SG=$SG"

################################################################################
# Step 3: Interface VPC endpoints for SSM (this is the private path to SSM)
################################################################################
echo ""
echo "[3/6] Creating interface VPC endpoints (ssm, ssmmessages, ec2messages)..."
for svc in ssm ssmmessages ec2messages; do
  EP=$(aws ec2 create-vpc-endpoint --vpc-id "$VPC" --vpc-endpoint-type Interface \
    --service-name "com.amazonaws.$REGION.$svc" \
    --subnet-ids "$SUBNET" --security-group-ids "$SG" --private-dns-enabled \
    --tag-specifications "$(tag_spec vpc-endpoint $PROJECT-$svc)" \
    --query 'VpcEndpoint.VpcEndpointId' --output text)
  echo "  $svc -> $EP"
done

################################################################################
# Step 4: Key pair (import public half of a locally generated key)
################################################################################
echo ""
echo "[4/6] Ensuring local key pair '$KEY_NAME'..."
mkdir -p "$(dirname "$KEY_PATH")"
if [ ! -f "$KEY_PATH" ]; then
  ssh-keygen -t ed25519 -f "$KEY_PATH" -N "" -C "$PROJECT" >/dev/null
fi
chmod 600 "$KEY_PATH"
aws ec2 import-key-pair --key-name "$KEY_NAME" \
  --public-key-material "fileb://$KEY_PATH.pub" >/dev/null 2>&1 || echo "  (key already imported)"

################################################################################
# Step 5: Launch the instance with NO public IP
################################################################################
echo ""
echo "[5/6] Launching private EC2 instance..."
AMI=$(aws ssm get-parameters \
  --names /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64 \
  --query 'Parameters[0].Value' --output text)
IID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
  --iam-instance-profile "Name=$INSTANCE_PROFILE" \
  --subnet-id "$SUBNET" --security-group-ids "$SG" \
  --no-associate-public-ip-address \
  --key-name "$KEY_NAME" \
  --metadata-options "HttpTokens=required,HttpEndpoint=enabled" \
  --tag-specifications "$(tag_spec instance $PROJECT)" \
  --query 'Instances[0].InstanceId' --output text)
echo "  AMI=$AMI  INSTANCE=$IID"

cat > "$STATE_FILE" <<EOF
REGION=$REGION
VPC=$VPC
SUBNET=$SUBNET
SG=$SG
INSTANCE=$IID
KEY_NAME=$KEY_NAME
KEY_PATH=$KEY_PATH
EOF

################################################################################
# Step 6: Wait for the instance to register with SSM as Online
################################################################################
echo ""
echo "[6/6] Waiting for SSM agent to come Online (via the endpoints)..."
for i in $(seq 1 40); do
  PING=$(aws ssm describe-instance-information \
    --filters "Key=InstanceIds,Values=$IID" \
    --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null || true)
  echo "  attempt $i: PingStatus=$PING"
  [ "$PING" = "Online" ] && break
  sleep 15
done

PUB=$(aws ec2 describe-instances --instance-ids "$IID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
PRIV=$(aws ec2 describe-instances --instance-ids "$IID" \
  --query 'Reservations[0].Instances[0].PrivateIpAddress' --output text)

echo ""
echo "=========================================="
echo "  DONE"
echo "  instance : $IID"
echo "  privateIp: $PRIV"
echo "  publicIp : ${PUB:-none}"
echo "  state    : $STATE_FILE"
echo ""
echo "  Next: ./connect.sh 'hostname && id'   (SSH over the SSM tunnel)"
echo "=========================================="
