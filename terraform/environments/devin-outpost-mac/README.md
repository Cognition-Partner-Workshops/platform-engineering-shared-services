# Devin Outpost — Mac worker (EC2)

Terraform to provision a **single Apple-silicon Mac EC2 instance** and configure it as a
[Devin Outpost](https://docs.devin.ai/cloud/outposts/overview) worker. The worker opens an
**outbound-only** connection to Devin Cloud, watches an outpost queue, and executes Devin
sessions locally on the Mac (useful for building/testing macOS & iOS apps).

## What it creates

| Resource | Notes |
|----------|-------|
| `aws_ec2_host` | **Dedicated Host** for the Mac instance type (required for all Mac instances). |
| `aws_instance` | `mac2-m2.metal` (Apple **M2**, 8 vCPU / 24 GiB) — the smallest Apple-silicon Mac with capacity in us-east-2. Latest Amazon macOS AMI (arm64_mac). `mac2.metal` (M1) is cheaper but had **no capacity** in any us-east-2 AZ at provisioning time. |
| `aws_key_pair` + `tls_private_key` | SSH keypair; private key written to `devin-outpost-mac.pem` (git-ignored). |
| `aws_security_group` | SSH (22) inbound, all outbound. |

Runs in the account's **default VPC** in `us-east-2b`.

## ⚠️ Cost / teardown caveat — read first

AWS **Mac Dedicated Hosts have a mandatory 24-hour minimum allocation.** The host cannot be
released (and continues billing, `mac2-m2` ≈ **$0.65/hr ≈ $15.60** for the 24h) until 24 hours
after allocation — even if you `terraform destroy` sooner. `terraform destroy` will **fail to
release the host** until that window passes; re-run it after 24h. Terminating the instance is
immediate, but the host charge continues.

Dedicated-host **capacity is scarce and varies by AZ/type**. There is no capacity-only dry-run
for `AllocateHosts` — a call either allocates a real (24h-billed) host or returns
`InsufficientHostCapacity`. If Terraform retries `aws_ec2_host` creation for a long time, the
type has no capacity in that AZ; switch `instance_type`/`availability_zone` (e.g. `mac2-m2.metal`
in `us-east-2b`/`c`).

## Usage

```bash
export AWS_ACCESS_KEY_ID=...        # credentials for account 599083837640
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-2

terraform init
# Recommended: lock SSH down to your IP
terraform apply -var='ssh_ingress_cidrs=["<YOUR_IP>/32"]'
```

Then configure the worker (see `bootstrap-worker.sh`):

```bash
IP=$(terraform output -raw public_ip)
scp -i devin-outpost-mac.pem bootstrap-worker.sh ec2-user@"$IP":
ssh -i devin-outpost-mac.pem ec2-user@"$IP" \
  "OUTPOST_NAME=<name> DEVIN_OUTPOSTS_TOKEN=<token> bash bootstrap-worker.sh"
```

The bootstrap script installs the Devin CLI + `ffmpeg` + Chrome, then registers a **launchd
LaunchDaemon** (`ai.devin.worker`, running as `ec2-user`) that keeps
`devin worker start --outpost=<name>` running (survives reboots and restarts on crash). Logs
land in `~/devin-worker/logs/`. Check status with
`sudo launchctl print system/ai.devin.worker | grep -E 'state|pid'`.

> A **LaunchDaemon** (system domain) is used instead of a LaunchAgent because EC2 Macs are
> headless by default — there is no logged-in Aqua/GUI session, so the `gui/<uid>` domain is
> unreachable. **Caveat:** browser / computer-use and screen-recording features need a
> logged-in window-server session; enable **auto-login** on the Mac if your sessions require
> them. Plain build/test/repo work does not.

Run the bootstrap **non-interactively** (as shown) so the CLI installer does not launch its
interactive `devin setup` prompt — the worker authenticates via the token, not a login.

### Get an outpost + token

Create the outpost in **Devin Cloud → Settings → Environment → Outposts** (platform = macOS)
and copy the one-time token, **or** with a v3 API token that has the
`account.outposts.orchestrator` scope:

```bash
devin worker outpost create <name> --platform macos --description "EC2 Mac worker"
```

## Teardown

```bash
terraform destroy   # re-run after the 24h host-allocation window if host release fails
```

## Variables

See `variables.tf`. Common overrides:

- `ssh_ingress_cidrs` — **lock this to your IP** (default is `0.0.0.0/0`).
- `availability_zone` — `us-east-2a|b|c` all offer `mac2.metal`.
- `instance_type` / `macos_ami_prefix` — e.g. `mac2-m2.metal`, or `amzn-ec2-macos-14` for Sonoma.
