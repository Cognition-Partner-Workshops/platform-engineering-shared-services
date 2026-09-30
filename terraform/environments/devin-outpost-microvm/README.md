# Devin Outposts on AWS Lambda MicroVMs

Runs [Devin Outposts](https://docs.devin.ai/cloud/outposts/quickstart) workers as
AWS Lambda MicroVMs: one ephemeral MicroVM per Devin session, created on demand
and terminated when the session ends. There is no always-on worker host, so the
idle cost of the stack is the reconciler's schedule plus a secret — roughly a
dollar a month.

This is the MicroVM analogue of
[`CognitionAI/devin-outpost-k8s`](https://github.com/CognitionAI/devin-outpost-k8s),
and deliberately borrows its architecture.

## How it works

```
Devin Outposts queue  (api.devin.ai/opbeta/outposts)
        |
        |  list / claim / renew / release        <- reconciler holds the account token
        v
reconciler Lambda  --- EventBridge, every minute
        |
        |  run-microvm, one per claimed session, connect token in the run hook payload
        v
worker MicroVM  ->  devin worker start --session <id>   (direct-serve mode)
        |
        |  outbound websocket
        v
outpost gateway  ->  the Devin session
```

The split between reconciler and worker is the important part, and it is taken
from the Kubernetes operator: **the reconciler is the only component holding the
account token.** A claim returns a `connect_token` and `gateway_url` scoped to a
single session; those are handed to the MicroVM through its `/run` hook payload,
and the worker then runs in direct-serve mode, where it serves exactly that one
session and never contacts the queue API. A compromised worker therefore leaks
one session, not the account.

## Mapping from the Kubernetes operator

| `devin-outpost-k8s` | here |
| --- | --- |
| Operator `Deployment` | `reconciler` Lambda on an EventBridge schedule |
| SSE watch on `/devins` | polling `list` (Lambda caps an invocation at 15 min) |
| `OutpostPool` CRD | Terraform variables |
| `plan.rs` | `plan()` in `reconciler/handler.py`, kept pure for the same reason |
| Worker `Pod` | MicroVM |
| Worker image `devin-cli:stable` | same image, as the `FROM` in `worker/Dockerfile` |
| Connect token via `secretKeyRef` | `runHookPayload` on `run-microvm` |
| Pod deletion | `terminate-microvm` |
| Owner references for GC | `terminate_untracked()` sweep + DynamoDB records |
| `restartPolicy: OnFailure` | `maximumDurationInSeconds` |
| `FilesystemSnapshot` resume policy | not used; see [Limits](#limits-and-trade-offs) |

## Prerequisites

- An outpost with **`platform: linux`**. The MicroVM base image is Amazon Linux
  2023; a macOS outpost will never match these workers.
- A service account token (`cog_...`) with the outposts machine scope, shown once
  when the outpost is created.
- Local Python with `boto3 >= 1.41` / `botocore >= 1.43`, the first versions
  carrying the `lambda-microvms` model. Point `python_bin` at it.
- A region where Lambda MicroVMs exist: `us-east-1`, `us-east-2`, `us-west-2`.

## Usage

```bash
export TF_VAR_outpost_id=outpost_env-xxxxxxxx
export TF_VAR_outpost_token=cog_xxxxxxxx      # kept out of the repo; only the reconciler reads it
export TF_VAR_python_bin=/path/to/venv/bin/python

terraform init
terraform apply
```

The first apply takes a few minutes: Lambda builds the worker image from
`worker/` and snapshots it.

Teardown drains any running MicroVMs before deleting the image, so a single
command is enough and leaves nothing billing:

```bash
terraform destroy
```

To watch it work:

```bash
aws logs tail /aws/lambda/devin-outpost-microvm-reconciler --follow
aws logs tail /devin/outpost/devin-outpost-microvm/workers --follow   # one stream per session
aws lambda invoke --function-name devin-outpost-microvm-reconciler /dev/stdout   # skip the schedule
```

## Putting workers in a VPC

Workers default to the AWS-managed `INTERNET_EGRESS` connector: plain internet,
no VPC, so they can reach nothing private. Set `egress_connector_arn` to a VPC
egress connector and the reconciler launches every worker through it, at which
point the VPC's subnets and security groups decide what a worker can reach.
`terraform/environments/devin-outpost-vpc-demo` builds such a VPC and
demonstrates the difference between a permitted and a restricted worker.

## Limits and trade-offs

- **8 hour ceiling per session.** `maximumDurationInSeconds` caps at 28800, and
  a MicroVM serves one session for its whole life, so that is also the longest a
  single session can run here. Sessions are expected to finish well inside it;
  a session that does not is killed rather than migrated.
- **Up to ~60s to pick up a session.** Lambda cannot hold the API's SSE watch
  stream open, so the reconciler polls on a schedule. Lower
  `reconcile_interval_minutes` for faster pickup, at proportionally more
  invocations. It must stay comfortably below the ~5 minute claim TTL.
- **No idle policy is set on the MicroVM.** Lambda measures idleness as absence
  of traffic on the MicroVM's *endpoint*, but a Devin worker only talks outbound
  to the gateway. Any idle policy here would suspend workers mid-session.
- **Suspend releases the MicroVM rather than snapshotting it.** This matches the
  operator's `NoSnapshot` policy: a suspended session is torn down and served by
  a fresh MicroVM when it resumes. Native `suspend-microvm`/`resume-microvm`
  would preserve memory and disk and is the obvious improvement, but `/resume`
  carries no payload, so resuming needs a second path to deliver the new
  connect token into the running MicroVM.
- **The MicroVM image is not a Terraform resource.** The AWS provider has none
  yet ([#48526](https://github.com/hashicorp/terraform-provider-aws/issues/48526)),
  so `scripts/microvm_image.py` runs from a `terraform_data` provisioner. It is
  idempotent, and its destroy provisioner is what drains and deletes the image,
  so the lifecycle still belongs to Terraform. Swap it for the real resources
  once they ship.

  The lifecycle is split across two `terraform_data` resources on purpose.
  `microvm_image` owns the image's *existence*, and its destroy step terminates
  every running MicroVM — so it is only ever replaced when the image identity
  changes. `microvm_image_version` publishes a new version when the worker
  changes, which running MicroVMs never see. Collapsing the two would make an
  ordinary worker deploy kill every in-flight session.

## Layout

| Path | |
| --- | --- |
| `main.tf` | S3, IAM, Secrets Manager, DynamoDB, CloudWatch, Lambda, EventBridge |
| `worker/Dockerfile` | worker image: the Devin CLI plus git, ffmpeg and chromium |
| `worker/supervisor.py` | serves the MicroVM lifecycle hooks, launches the worker |
| `reconciler/handler.py` | queue client, `plan()`, and the MicroVM lifecycle |
| `reconciler/test_handler.py` | `python -m pytest reconciler/test_handler.py` |
| `scripts/microvm_image.py` | create/update/delete the MicroVM image |
