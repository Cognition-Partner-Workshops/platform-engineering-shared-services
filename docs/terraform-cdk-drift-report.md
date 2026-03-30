# Terraform vs CDK Drift Report

> **Generated:** 2026-03-30  
> **Repository:** `platform-engineering-shared-services`  
> **Scope:** Comparison of all infrastructure resources defined in `terraform/` and `cdk/` directories

---

## Summary

Both the Terraform and CDK codebases define the same **core set of five resource categories**: VPC/Networking, EKS Cluster, ECR Repositories, DNS (Route 53), and Kubernetes Namespaces. The two frameworks are **largely aligned** in intent and resource coverage, but there are notable **configuration-level drifts** in the staging and production environments, as well as structural differences in how each framework manages state, removal policies, and IAM.

| Metric | Count |
|---|---|
| Resource categories defined in both | 5 |
| Resource categories only in Terraform | 0 |
| Resource categories only in CDK | 0 |
| Environments with configuration drift | 2 (staging, prod) |
| Environments that match closely | 1 (dev) |

---

## Environments Compared

Both frameworks define three environments. The table below shows how each environment is declared:

| Environment | Terraform Location | CDK Location |
|---|---|---|
| Dev | `terraform/environments/dev/main.tf` | `cdk/bin/cdk.ts` &rarr; `WorkshopPlatformDev` |
| Staging | `terraform/environments/staging/main.tf` | `cdk/bin/cdk.ts` &rarr; `WorkshopPlatformStaging` |
| Production | `terraform/environments/prod/main.tf` | `cdk/bin/cdk.ts` &rarr; `WorkshopPlatformProd` |

---

## Resources Defined in Both

| Resource Type | Terraform Location | CDK Location | Status |
|---|---|---|---|
| VPC / Networking | `terraform/modules/networking/` | `cdk/lib/constructs/networking.ts` | Aligned (minor structural differences) |
| EKS Cluster | `terraform/modules/eks-cluster/` | `cdk/lib/constructs/eks-cluster.ts` | Aligned (CDK adds explicit IAM master role) |
| ECR Repositories | `terraform/modules/ecr/` | `cdk/lib/constructs/ecr-repositories.ts` | Aligned (identical repo list and lifecycle rules) |
| Route 53 DNS Zone | `terraform/modules/dns/` | `cdk/lib/constructs/dns-zone.ts` | Aligned (neither dev environment instantiates it) |
| K8s Namespaces | `terraform/modules/namespaces/` | `cdk/lib/constructs/k8s-namespaces.ts` | Aligned (identical defaults for quotas/limits) |

---

## Resources Only in Terraform

| Resource Type | Terraform Location | Notes |
|---|---|---|
| S3 Backend + DynamoDB Lock | `terraform/environments/*/main.tf` (backend block) | Terraform state management (`workshop-terraform-state-599083837640` S3 bucket, `workshop-terraform-lock` DynamoDB table). CDK uses CloudFormation natively for state, so this is framework-specific and not a real drift. |
| Bootstrap Resources | `terraform/bootstrap/main.tf` | Bootstrapping for the Terraform state backend. No CDK equivalent needed. |

---

## Resources Only in CDK

| Resource Type | CDK Location | Notes |
|---|---|---|
| IAM Role (`ClusterAdminRole`) | `cdk/lib/constructs/eks-cluster.ts` (lines 67-71) | CDK explicitly creates a `{clusterName}-admin` IAM Role assumed by the account root principal and attaches it as `mastersRole`. Terraform relies on `enable_cluster_creator_admin_permissions = true` in the community EKS module, which grants the provisioning principal admin access without a dedicated named role. |
| KubectlV31 Lambda Layer | `cdk/lib/constructs/eks-cluster.ts` (line 76) | CDK uses a Lambda-backed custom resource with `KubectlV31Layer` to run `kubectl` commands for K8s manifest management. Terraform uses the `kubernetes` provider directly. |
| RemovalPolicy.DESTROY on all resources | Multiple CDK constructs | CDK explicitly sets `RemovalPolicy.DESTROY` and `emptyOnDelete: true` on VPC, ECR repos, IAM roles, and DNS zone. Terraform's `force_delete = true` on ECR repos is the closest equivalent; VPC and DNS use `force_destroy = true` in Terraform's DNS module. |

---

## Configuration Drift (Same Resource, Different Settings)

### 1. Networking (VPC)

| Parameter | Terraform (dev) | CDK (dev) | Match? |
|---|---|---|---|
| VPC Name | `workshop-dev` | `workshop-dev` | Yes |
| VPC CIDR | `10.0.0.0/16` | `10.0.0.0/16` (default) | Yes |
| Availability Zones | 2 (`us-east-1a`, `us-east-1b`) | 2 (`maxAzs: 2`) | Yes |
| NAT Gateways | 1 (`single_nat_gateway = true`) | 1 (`natGateways: 1`) | Yes |
| DNS Hostnames | Enabled | Enabled | Yes |
| DNS Support | Enabled | Enabled | Yes |
| Subnet Tags (ELB) | `kubernetes.io/role/elb` on public, `kubernetes.io/role/internal-elb` on private | Same tags applied | Yes |
| Subnet CIDRs | Explicit defaults: `10.0.1-3.0/24` (private), `10.0.101-103.0/24` (public) | Auto-calculated by CDK VPC construct (`cidrMask: 24`) | **Minor** -- functionally equivalent but CIDRs will differ |
| Terraform Module Source | `terraform-aws-modules/vpc/aws ~> 5.0` | `aws-cdk-lib/aws-ec2.Vpc` (L2 construct) | N/A |

| Parameter | Terraform (staging) | CDK (staging) | Match? |
|---|---|---|---|
| VPC CIDR | `10.1.0.0/16` | `10.0.0.0/16` (default) | **DRIFT** |
| Availability Zones | 3 | 2 (`maxAzs: 2`) | **DRIFT** |
| NAT Gateways | Multiple (`single_nat_gateway = false`) | 1 (`natGateways: 1`) | **DRIFT** |

| Parameter | Terraform (prod) | CDK (prod) | Match? |
|---|---|---|---|
| VPC CIDR | `10.2.0.0/16` | `10.0.0.0/16` (default) | **DRIFT** |
| Availability Zones | 3 | 3 (`maxAzs: 3`) | Yes |
| NAT Gateways | Multiple (`single_nat_gateway = false`) | 2 (`natGateways: 2`) | **Possible drift** -- Terraform creates one per AZ (3), CDK creates exactly 2 |

### 2. EKS Cluster

| Parameter | Terraform (dev) | CDK (dev) | Match? |
|---|---|---|---|
| Cluster Name | `workshop-dev` | `workshop-dev` | Yes |
| K8s Version | `1.31` | `V1_31` (default) | Yes |
| Instance Types | `t3.medium` | `T3.MEDIUM` | Yes |
| Min Nodes | 1 | 1 | Yes |
| Max Nodes | 3 | 3 | Yes |
| Desired Nodes | 2 | 2 | Yes |
| Public Endpoint | Yes | Yes (`EndpointAccess.PUBLIC`) | Yes |
| Force Update | Yes | Yes (`forceUpdate: true`) | Yes |
| Admin Access | `enable_cluster_creator_admin_permissions = true` | Explicit `mastersRole` IAM role | **Structural difference** |
| Terraform Module | `terraform-aws-modules/eks/aws ~> 20.0` | `aws-cdk-lib/aws-eks.Cluster` (L2) | N/A |

| Parameter | Terraform (staging) | CDK (staging) | Match? |
|---|---|---|---|
| Instance Types | `t3.large` | `T3.MEDIUM` | **DRIFT** |
| Min Nodes | 2 | 2 | Yes |
| Max Nodes | 5 | 5 | Yes |
| Desired Nodes | 3 | 3 | Yes |

| Parameter | Terraform (prod) | CDK (prod) | Match? |
|---|---|---|---|
| Instance Types | `t3.xlarge` | `T3.LARGE` | **DRIFT** |
| Min Nodes | 3 | 3 | Yes |
| Max Nodes | 10 | 10 | Yes |
| Desired Nodes | 5 | 3 | **DRIFT** |

### 3. ECR Repositories

| Parameter | Terraform (dev) | CDK (dev) | Match? |
|---|---|---|---|
| Repository Names | 6 repos (web-frontend, api-gateway, order-service, inventory-service, customer-service, product-service) | Same 6 repos | Yes |
| Scan on Push | Yes | Yes | Yes |
| Tag Mutability | MUTABLE | MUTABLE | Yes |
| Lifecycle: Keep 10 tagged | Yes (`v`, `release` prefixes) | Yes (`v`, `release` prefixes) | Yes |
| Lifecycle: Expire untagged after 7 days | Yes | Yes | Yes |
| Force Delete / Empty on Delete | `force_delete = true` | `removalPolicy: DESTROY`, `emptyOnDelete: true` | Functionally equivalent |

> ECR configuration is **fully aligned** across all environments. Both Terraform staging/prod and CDK staging/prod define the same 6 repositories.

### 4. Kubernetes Namespaces

| Parameter | Terraform (dev) | CDK (dev) | Match? |
|---|---|---|---|
| Namespaces | `decomposition-dev`, `decomposition-staging` | `decomposition-dev`, `decomposition-staging` | Yes |
| Labels | `managed-by: terraform` | `managed-by: cdk` | **Expected difference** |
| Resource Quota (default) | CPU req: 2, Mem req: 4Gi, CPU limit: 4, Mem limit: 8Gi, Pods: 20 | Same defaults | Yes |
| LimitRange (default) | Container default: 500m/256Mi, default request: 100m/128Mi | Same defaults | Yes |

| Environment | Terraform Namespaces | CDK Namespaces | Match? |
|---|---|---|---|
| Dev | `decomposition-dev`, `decomposition-staging` | `decomposition-dev`, `decomposition-staging` | Yes |
| Staging | Not defined (staging env has no namespace module) | `decomposition-staging` | **DRIFT** |
| Prod | Not defined (prod env has no namespace module) | `decomposition-prod` | **DRIFT** |

### 5. DNS Zone (Route 53)

Both define an optional DNS zone construct. Neither the dev Terraform environment nor the CDK dev stack instantiate it (`domainName` is not passed in CDK; no `dns` module block in Terraform dev `main.tf`). **No drift** for the DNS zone -- it is dormant in both.

---

## Drift Summary Table

| Environment | Resource | Parameter | Terraform Value | CDK Value | Severity |
|---|---|---|---|---|---|
| Staging | VPC | CIDR | `10.1.0.0/16` | `10.0.0.0/16` | **High** -- would create overlapping VPCs if both applied |
| Staging | VPC | AZs | 3 | 2 | Medium |
| Staging | VPC | NAT Gateways | Multiple (1 per AZ) | 1 | Medium |
| Staging | EKS | Instance Type | `t3.large` | `t3.medium` | **High** -- undersized for staging workloads |
| Prod | VPC | CIDR | `10.2.0.0/16` | `10.0.0.0/16` | **High** -- would create overlapping VPCs |
| Prod | VPC | NAT Gateways | 3 (1 per AZ) | 2 | Low |
| Prod | EKS | Instance Type | `t3.xlarge` | `t3.large` | **High** -- undersized for production |
| Prod | EKS | Desired Nodes | 5 | 3 | Medium |
| Staging | Namespaces | Defined? | No namespace module | `decomposition-staging` | Medium |
| Prod | Namespaces | Defined? | No namespace module | `decomposition-prod` | Medium |
| All | Namespaces | managed-by label | `terraform` | `cdk` | Low (expected) |

---

## Additional Observations

### 1. Target Account and Region

Both frameworks target the **same AWS region** (`us-east-1`) by default. Terraform explicitly references AWS account `599083837640` in the S3 backend bucket name. CDK uses `CDK_DEFAULT_ACCOUNT` from the environment, which should resolve to the same account at deploy time.

**Risk:** If both were applied to the same account, VPC CIDR overlaps in staging/prod (`10.0.0.0/16` from CDK vs `10.1.0.0/16` and `10.2.0.0/16` from Terraform) would cause deployment failures.

### 2. Completeness

| Aspect | Terraform | CDK | Assessment |
|---|---|---|---|
| Dev environment | Fully defined (VPC, EKS, ECR, Namespaces) | Fully defined (VPC, EKS, ECR, DNS optional, Namespaces) | CDK matches Terraform |
| Staging environment | VPC + EKS only | Full stack (VPC, EKS, ECR, Namespaces) | **CDK is more complete** |
| Prod environment | VPC + EKS only | Full stack (VPC, EKS, ECR, Namespaces) | **CDK is more complete** |
| Kubernetes provider | Native `hashicorp/kubernetes` provider | Lambda-backed `kubectl` via custom resources | Different mechanisms |
| State management | S3 + DynamoDB locking | CloudFormation (native) | Framework-specific |
| Teardown strategy | `force_delete` on ECR, `force_destroy` on DNS | `RemovalPolicy.DESTROY` + `emptyOnDelete` globally | CDK is more thorough |

### 3. Conflicting Resource Names

If both IaC frameworks were applied to the same account/region, the following naming conflicts would occur:

- **EKS Cluster:** Both use `workshop-{env}` -- direct conflict
- **VPC:** Both use `workshop-{env}` as VPC name -- direct conflict
- **ECR Repos:** Both create the same 6 repository names -- direct conflict
- **K8s Namespaces:** Both create `decomposition-dev` and `decomposition-staging` -- would collide

**These two codebases must never be applied simultaneously to the same AWS account.**

### 4. CDK Creates Additional IAM Resources

CDK's EKS construct creates:
- A `{clusterName}-admin` IAM Role (used as `mastersRole`)
- Lambda execution roles for the `kubectl` layer
- Additional CloudFormation custom resource roles

Terraform's community EKS module relies on `enable_cluster_creator_admin_permissions` and does not create a separate named admin role.

### 5. Structural Differences in K8s Resource Management

- **Terraform** uses the `hashicorp/kubernetes` provider to create Namespace, ResourceQuota, and LimitRange resources directly.
- **CDK** uses `cluster.addManifest()` which creates a CloudFormation custom resource backed by a Lambda function that runs `kubectl apply`.

Both achieve the same result but through different mechanisms with different failure modes.

---

## Recommendations

### 1. Choose a Single Source of Truth

The CDK codebase is **more complete** (all three environments have full resource definitions) and **more modern** (uses CDK v2 with current constructs, has cleaner teardown semantics). Terraform's staging and prod environments are incomplete (missing ECR and Namespace modules).

**Recommendation:** Adopt CDK as the primary IaC framework and deprecate the Terraform definitions, or bring Terraform up to parity.

### 2. Fix Critical Configuration Drift Immediately

Before either framework is used in staging/prod, resolve:

| Fix | Action |
|---|---|
| Staging VPC CIDR | Update CDK staging to `10.1.0.0/16` (or update Terraform to `10.0.0.0/16`) |
| Staging AZs | Align to 3 AZs in CDK staging or reduce Terraform to 2 |
| Staging NAT Gateways | Align CDK to `natGateways: 3` or Terraform to `single_nat_gateway = true` |
| Staging EKS instance type | Update CDK staging to `T3.LARGE` to match Terraform |
| Prod VPC CIDR | Update CDK prod to `10.2.0.0/16` |
| Prod EKS instance type | Update CDK prod to `T3.XLARGE` to match Terraform |
| Prod EKS desired nodes | Update CDK prod to `nodeDesiredSize: 5` to match Terraform |

### 3. Complete Terraform Staging/Prod Definitions

If Terraform is retained as the source of truth, add ECR and Namespace modules to `terraform/environments/staging/main.tf` and `terraform/environments/prod/main.tf`.

### 4. Prevent Dual Application

Add a prominent warning to both `terraform/README.md` and `cdk/README.md` (or the repo root `README.md`) that these two IaC codebases must **never be applied to the same AWS account simultaneously**.

### 5. Unify the `managed-by` Label

The namespace `managed-by` label differs (`terraform` vs `cdk`). While this is expected when running one or the other, any tooling that filters by this label should be updated when switching frameworks.
