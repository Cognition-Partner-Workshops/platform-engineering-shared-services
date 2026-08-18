# AWS Cost Cleanup Log

Running log of billable AWS resources identified as waste, and their remediation status.

**Account:** `599083837640` · **Regions in use:** `us-east-1`, `us-east-2`

Baseline spend at time of audit: **~$44.08/day (~$1,322/month)**, measured from Cost Explorer
unblended cost for 2026-07-25 (the last full day, excluding a one-off EC2 Mac dedicated-host spike).

## Status legend

| Status | Meaning |
| --- | --- |
| `OPEN` | Confirmed waste, not yet remediated |
| `NEEDS-DECISION` | Costly, but removing it may drop functionality — owner must confirm |
| `DONE` | Remediated, cost verified as stopped |

## Items

### 1. EKS `otterworks-dev` is on an extended-support Kubernetes version

- **Status:** `OPEN`
- **Cost:** $12.00/day (~$360/month) — *penalty only, on top of the $2.40/day control plane*
- **Region:** us-east-1
- **Detail:** The cluster runs Kubernetes `1.32` with `upgradePolicy.supportType = EXTENDED`.
  Versions past standard support bill the control plane at $0.60/hr instead of $0.10/hr, so the
  cluster costs 6x with no added capability.
- **Action:** Upgrade the cluster to a version still in standard support, then set
  `upgradePolicy.supportType = STANDARD` so it cannot silently re-enter extended support.
- **Risk:** Requires a control-plane upgrade and node-group roll. Schedule it; do not do it blind.

### 2. OpenSearch Serverless collection `otterworks-search-os1` bills at its OCU floor

- **Status:** `NEEDS-DECISION`
- **Cost:** $11.52/day (~$346/month)
- **Region:** us-east-1
- **Detail:** 1 indexing OCU + 1 search OCU are billed continuously. OpenSearch Serverless has no
  scale-to-zero, so an idle collection costs the same as a used one.
- **Action:** If otterworks search is dormant, delete the collection (snapshot first if the index
  matters). If it is needed only for demos, recreate it on demand rather than leaving it running.

### 3. Three orphaned Classic Load Balancers with zero backends

- **Status:** `OPEN`
- **Cost:** ~$1.80/day in LB hours plus their public IPv4 addresses (~$54/month total)
- **Region:** us-east-1
- **Detail:** Left behind by deleted Kubernetes `Service` objects — the LBs still carry
  `kubernetes.io/cluster/otterworks-dev = owned` tags but have no registered instances, and have
  been running since 2026-06-23.

  | Load balancer | Orphaned from |
  | --- | --- |
  | `ae7575c5ae1b549ea96ba108b314ca35` | `otterworks/web-app` |
  | `ad381b39e74e64c3b9caa4c6f5328ce1` | `otterworks/api-gateway` |
  | `a0aa8ca8fa35c446fba1bd60e1505405` | `otterworks/admin-dashboard` |

- **Action:** Delete the three load balancers. Longer term, make tenant teardown delete
  `Service type=LoadBalancer` objects before the namespace so the in-tree controller can clean up.

### 4. Lambda provisioned concurrency on a demo function

- **Status:** `OPEN`
- **Cost:** $2.16/day (~$65/month)
- **Region:** us-east-2
- **Detail:** `report-service-lam1:live` holds 3 units of provisioned concurrency, billed
  continuously whether or not the function is invoked.
- **Action:** Delete the provisioned-concurrency config. Demo traffic does not need warm starts.

### 5. RDS Proxies cost more than the databases they front

- **Status:** `NEEDS-DECISION`
- **Cost:** $1.44/day (~$43/month)
- **Region:** us-east-2
- **Detail:** Proxies `report-service-lam1` and `report-service-ea3c` front a `db.t4g.small` and a
  `db.t4g.micro` respectively. Proxy cost scales with the underlying instance vCPU floor, so on
  micro/small instances the proxy is the larger line item.
- **Action:** Drop the proxies unless connection pooling is genuinely being demonstrated.

### 6. Unattached EBS volumes

- **Status:** `OPEN`
- **Cost:** ~$2/month
- **Region:** us-east-1
- **Detail:** `vol-044ed8c99191e1d89` (20 GiB) and `vol-0109df98e75c576f4` (5 GiB) are in
  `available` state with no attachment.
- **Action:** Snapshot if the contents matter, then delete.

### 7. Long-stopped demo EC2 instances still paying for storage

- **Status:** `NEEDS-DECISION`
- **Cost:** ~$10/month of gp3 storage plus one idle Elastic IP
- **Region:** us-east-2
- **Detail:** `MagentoServer` (100 GiB), `Jenkins` (20 GiB) and `Subversion` (8 GiB) are stopped.
  Stopped instances are free, but their root volumes and the `MagentoServerEIP` are not.
- **Action:** If these demos are retired, terminate the instances and release the EIP. If they are
  kept for occasional use, create AMIs and delete the running volumes.

### 8. Public IPv4 addresses

- **Status:** `OPEN` (largely resolved by items 3 and 7)
- **Cost:** $2.04/day (~$61/month) across 17 addresses
- **Detail:** Most are attached to load balancers. Deleting the orphaned LBs in item 3 and the idle
  EIP in item 7 removes the avoidable share; the rest are in active use.

### 9. Devin Outpost Mac dedicated hosts

- **Status:** `DONE` (2026-07-25)
- **Cost avoided:** ~$0.65/hr per host
- **Region:** us-east-2
- **Detail:** One `mac2-m2.metal` worker host plus three hosts allocated in error during a capacity
  probe. All four were released once the mandatory 24-hour minimum allocation period expired; the
  EC2 instance, security group, key pair and EBS volume were destroyed with `terraform destroy`.
- **Note:** `AllocateHosts` has no capacity-only dry run, so probing Mac capacity allocates real
  hosts that cannot be released for 24 hours. Check
  `describe-instance-type-offerings` and let Terraform surface `InsufficientHostCapacity` instead.

## Estimated savings

| Bucket | Monthly |
| --- | --- |
| Unambiguous waste (items 1, 3, 4, 6) | ~$481 |
| Requires an owner decision (items 2, 5, 7) | ~$399 |
| **Total addressable** | **~$880** |

## Recurring checks

These are the queries that produced this log; re-run them periodically.

```bash
# Spend by service for the last full day
aws ce get-cost-and-usage --region us-east-1 \
  --time-period Start=<yesterday>,End=<today> \
  --granularity DAILY --metrics UnblendedCost \
  --group-by Type=DIMENSION,Key=SERVICE

# Load balancers with no registered backends
aws elb describe-load-balancers --region us-east-1 \
  --query "LoadBalancerDescriptions[?length(Instances)==\`0\`].LoadBalancerName"

# Unattached EBS volumes
aws ec2 describe-volumes --region us-east-1 --filters Name=status,Values=available \
  --query "Volumes[].[VolumeId,Size]"

# EKS clusters paying the extended-support premium
aws eks describe-cluster --region us-east-1 --name <cluster> \
  --query "cluster.{version:version,support:upgradePolicy.supportType}"

# Functions holding provisioned concurrency
aws lambda list-provisioned-concurrency-configs --region us-east-2 --function-name <fn>
```
