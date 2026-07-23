# Private EC2 + SSM reverse tunnel (SSH over port forwarding)

A minimal, self-contained demo of connecting to an EC2 instance that has **no
public IP, no Internet Gateway route, and no NAT** — reachable only through the
AWS Systems Manager (SSM) agent's outbound "reverse tunnel."

The instance opens an **outbound** connection to SSM (over interface VPC
endpoints). We then use `aws ssm start-session` with the
`AWS-StartPortForwardingSession` document to forward a local port to port `22`
on the instance and `ssh` back down that same tunnel. Nothing inbound is ever
opened to the instance.

```
   your laptop / CI box                         AWS (private subnet, no IGW/NAT)
 ┌──────────────────────┐                     ┌──────────────────────────────┐
 │ ssh 127.0.0.1:2222   │                     │  EC2 (private IP only)        │
 │   │                  │   SSM data channel  │   ├─ sshd :22                 │
 │   ▼                  │  (outbound from EC2)│   └─ ssm-agent ──┐            │
 │ aws ssm start-session│◀════════════════════╪══════════════════┘            │
 │  PortForwarding :2222 │                     │  interface VPC endpoints:     │
 └──────────────────────┘                     │  ssm / ssmmessages / ec2msg   │
                                              └──────────────────────────────┘
```

## Why interface VPC endpoints (not an IGW)?

An instance with **no public IP** cannot reach the internet through an Internet
Gateway — an IGW only routes traffic for instances that have a public IP (or you
add a NAT gateway). To keep the instance *fully private* while still letting the
SSM agent phone home, this demo attaches three **interface VPC endpoints**
(`ssm`, `ssmmessages`, `ec2messages`). Result: the route table has only the
`local` route, there is zero internet egress, and SSM still works. That is what
best showcases the reverse-tunnel behavior.

## Prerequisites

- AWS CLI v2 with credentials exported (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`)
- The [Session Manager plugin](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html) for the AWS CLI
- Permissions for EC2, VPC endpoints, and SSM. If the `EC2-SSM-InstanceProfile`
  instance profile does not already exist, `provision.sh` creates it (needs IAM
  permissions).

## Usage

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export AWS_REGION=us-east-2          # optional (default us-east-2)

./provision.sh                       # build VPC, endpoints, and the instance
./connect.sh 'hostname && id'        # run a remote command over the tunnel
./connect.sh                         # or open an interactive SSH shell
./teardown.sh                        # delete everything when done
```

`provision.sh` writes non-secret resource IDs to `.demo-state` (git-ignored).
The SSH key pair is generated under `~/.ssh/ssm-portfwd-demo` and never
committed.

## Verifying it really is private

Once connected via `./connect.sh`, from inside the instance:

```bash
hostname -I                          # only a 10.0.x.x private address
curl -m3 https://example.com         # times out — no internet egress
TOKEN=$(curl -s -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/public-ipv4   # 404 — no public IP
```

And from your machine:

```bash
aws ec2 describe-security-groups \
  --filters Name=group-name,Values=ssm-portfwd-demo-sg \
  --query 'SecurityGroups[0].IpPermissions'             # only 443 from the VPC; no :22
```

## Files

| File | Purpose |
|------|---------|
| `provision.sh` | Create the VPC, private subnet, SG, SSM interface endpoints, key pair, and instance. Waits until SSM reports the instance `Online`. |
| `connect.sh` | Open an SSM port-forwarding session and SSH through it (interactive or one-off command). |
| `teardown.sh` | Delete all resources tagged `Project=ssm-portfwd-demo`. |
