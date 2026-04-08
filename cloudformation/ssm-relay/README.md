# SSM Relay Stack — Private VPC Port-Forwarding Demo

Provisions a fully private VPC (no Internet Gateway, no NAT Gateway) with an EC2 relay box running a mock JFrog Artifactory server. Demonstrates how AWS Systems Manager (SSM) port-forwarding enables secure data-plane connectivity to private resources without any inbound network access.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Your Machine (Devin)                      │
│                                                              │
│  AWS CLI + Session Manager Plugin                           │
│  IAM User: ssm-relay-demo-ssm-portforward-user              │
│  (can ONLY port-forward to the specific relay instance)      │
│                                                              │
│  localhost:8081 ──► SSM Port-Forwarding Session              │
└──────────────┬──────────────────────────────────────────────┘
               │ (encrypted SSM channel over HTTPS)
               ▼
┌─────────────────────────────────────────────────────────────┐
│  AWS VPC (10.200.0.0/16) — NO Internet Gateway              │
│                                                              │
│  ┌─────────────────────┐    ┌──────────────────────────┐    │
│  │  VPC Endpoints       │    │  Private Subnet A         │    │
│  │  ├─ ssm              │    │  ┌──────────────────────┐ │    │
│  │  ├─ ssmmessages      │◄──►│  │  EC2 Relay Box       │ │    │
│  │  ├─ ec2messages      │    │  │  (Amazon Linux 2023)  │ │    │
│  │  └─ s3 (gateway)     │    │  │                       │ │    │
│  └─────────────────────┘    │  │  nginx :8081           │ │    │
│                              │  │  Mock Artifactory     │ │    │
│                              │  │  ├─ /api/system/ping  │ │    │
│                              │  │  ├─ /api/system/ver.  │ │    │
│                              │  │  └─ /api/repositories │ │    │
│                              │  └──────────────────────┘ │    │
│                              └──────────────────────────┘    │
│                                                              │
│  Security Groups:                                           │
│  ├─ Relay SG: ingress only from VPC CIDR on port 8081       │
│  └─ VPC Endpoint SG: HTTPS (443) from VPC CIDR             │
└─────────────────────────────────────────────────────────────┘
```

## What This Demonstrates

| Capability | How |
|---|---|
| **Private VPC with no ingress** | No IGW, no NAT, no public subnets — zero internet-initiated traffic |
| **SSM without internet** | VPC Interface Endpoints for `ssm`, `ssmmessages`, `ec2messages` |
| **Package installation in airgapped VPC** | S3 Gateway Endpoint enables `dnf` to reach Amazon Linux repos |
| **Fine-grained IAM** | Dedicated IAM user scoped to `ssm:StartSession` on one instance + one SSM document |
| **Data-plane connectivity** | SSM port-forwarding tunnels localhost traffic to private resources |
| **Mock Artifactory** | nginx serving realistic JFrog Artifactory API responses on port 8081 |

## Resources Created

| Resource | Type | Purpose |
|---|---|---|
| VPC | `AWS::EC2::VPC` | Isolated network (10.200.0.0/16) |
| Private Subnet A/B | `AWS::EC2::Subnet` | Two AZs, no public IPs |
| Route Table | `AWS::EC2::RouteTable` | Local routes only |
| S3 Gateway Endpoint | `AWS::EC2::VPCEndpoint` | Package manager access (dnf/yum) |
| SSM Interface Endpoints (x3) | `AWS::EC2::VPCEndpoint` | SSM agent registration + sessions |
| VPC Endpoint Security Group | `AWS::EC2::SecurityGroup` | HTTPS from VPC CIDR |
| Relay Security Group | `AWS::EC2::SecurityGroup` | Port 8081 from VPC CIDR only |
| EC2 Instance Role | `AWS::IAM::Role` | `AmazonSSMManagedInstanceCore` |
| EC2 Instance Profile | `AWS::IAM::InstanceProfile` | Attached to relay box |
| EC2 Relay Instance | `AWS::EC2::Instance` | Amazon Linux 2023 + nginx |
| IAM Port-Forward User | `AWS::IAM::User` | Fine-grained SSM-only permissions |
| IAM Policy | `AWS::IAM::Policy` | Scoped to specific instance + document |
| IAM Access Key | `AWS::IAM::AccessKey` | Credentials for the port-forward user |

## Quick Start

### Prerequisites

- AWS CLI v2
- [Session Manager Plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html)
- An AWS account with permissions to create CloudFormation stacks with IAM resources

### Deploy

```bash
aws cloudformation create-stack \
  --stack-name ssm-relay-demo \
  --template-body file://template.yaml \
  --capabilities CAPABILITY_NAMED_IAM \
  --region us-east-1
```

### Get Outputs

```bash
aws cloudformation describe-stacks \
  --stack-name ssm-relay-demo \
  --query 'Stacks[0].Outputs' \
  --output table
```

### Connect

```bash
# Export the fine-grained user credentials from stack outputs
export AWS_ACCESS_KEY_ID=<SsmPortForwardAccessKeyId>
export AWS_SECRET_ACCESS_KEY=<SsmPortForwardSecretAccessKey>
export AWS_DEFAULT_REGION=us-east-1

# Establish SSM port-forwarding tunnel
aws ssm start-session \
  --target <RelayInstanceId> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8081"],"localPortNumber":["8081"]}'
```

### Verify Connectivity

In a separate terminal:

```bash
# Health check
curl -s http://localhost:8081/artifactory/api/system/ping
# → OK

# Version info
curl -s http://localhost:8081/artifactory/api/system/version | jq
# → {"version":"7.77.0","revision":"mock-ssm-relay-demo",...}

# Repository list
curl -s http://localhost:8081/artifactory/api/repositories | jq
# → [{...libs-release-local...}, {...npm-remote...}, {...docker-local...}]
```

### Teardown

```bash
aws cloudformation delete-stack --stack-name ssm-relay-demo --region us-east-1
```

## IAM Policy Details

The `ssm-relay-demo-ssm-portforward-user` has the minimum permissions needed:

```json
{
  "Statement": [
    {
      "Sid": "AllowStartSessionOnInstance",
      "Effect": "Allow",
      "Action": ["ssm:StartSession"],
      "Resource": ["arn:aws:ec2:REGION:ACCOUNT:instance/INSTANCE_ID"],
      "Condition": {
        "BoolIfExists": {
          "ssm:SessionDocumentAccessCheck": "true"
        }
      }
    },
    {
      "Sid": "AllowPortForwardDocumentOnly",
      "Effect": "Allow",
      "Action": ["ssm:StartSession"],
      "Resource": ["arn:aws:ssm:REGION::document/AWS-StartPortForwardingSession"]
    },
    {
      "Sid": "AllowTerminateOwnSession",
      "Effect": "Allow",
      "Action": ["ssm:TerminateSession", "ssm:ResumeSession"],
      "Resource": ["arn:aws:ssm:REGION:ACCOUNT:session/${aws:username}-*"]
    },
    {
      "Sid": "AllowDescribeInstances",
      "Effect": "Allow",
      "Action": [
        "ssm:DescribeInstanceInformation",
        "ssm:GetConnectionStatus",
        "ssm:DescribeSessions",
        "ec2:DescribeInstances"
      ],
      "Resource": "*"
    }
  ]
}
```

This user **cannot**:
- Start interactive shell sessions (only port-forwarding)
- Target any other instance
- Use any other SSM document
- Terminate other users' sessions

## Devin Environment Integration

To use this as part of Devin's environment initialization, add to the `initialize` section:

```bash
# Install SSM Session Manager Plugin
curl -s "https://s3.amazonaws.com/session-manager-downloads/plugin/latest/ubuntu_64bit/session-manager-plugin.deb" \
  -o /tmp/session-manager-plugin.deb && sudo dpkg -i /tmp/session-manager-plugin.deb
```

And to the `maintenance` section (requires SSM credentials as Devin secrets):

```bash
# Establish SSM tunnel to private Artifactory
aws ssm start-session \
  --target <INSTANCE_ID> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8081"],"localPortNumber":["8081"]}' \
  --region us-east-1 &
```
