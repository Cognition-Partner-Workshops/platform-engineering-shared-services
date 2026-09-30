# Devin Outpost MicroVM — VPC segmentation demo

A throwaway VPC that proves what a Devin MicroVM worker can and cannot reach. It
stands up a private HTTP server plus two Lambda network connectors into the same
private subnets, differing only in the security group they attach to the worker:

| Connector | Worker security group | Reaches the private server | Reaches the internet |
|---|---|---|---|
| `trusted` | egress anywhere; allowed by the server's ingress rule | yes | yes, via NAT |
| `restricted` | egress limited to 443/tcp + 53/udp | no | yes, via NAT |

Segmentation is enforced by the VPC, not by the worker: the server's security
group only admits the trusted worker group, so the restricted worker has no
route to port 8080 even though both connectors live in the same subnets.

By default `devin-outpost-microvm` workers use the AWS-managed `INTERNET_EGRESS`
connector — plain internet, no VPC at all. Setting `egress_connector_arn` on
that stack is what places workers inside this VPC.

## Resources

VPC (`10.42.0.0/16`), two public and two private subnets, an internet gateway, a
NAT gateway + EIP, a `t4g.nano` Amazon Linux instance serving `server_banner` on
`server_port`, three security groups, an operator IAM role, and the two network
connectors.

The AWS provider has no `network connector` resource, so `scripts/network_connector.py`
drives `lambda-core` `CreateNetworkConnector` / `DeleteNetworkConnector` from a
`terraform_data` resource with a destroy provisioner. It needs a boto3 new enough
to know the `lambda-core` model; point `python_bin` at that interpreter.

## Run the demo

```bash
export TF_VAR_python_bin=/path/to/python-with-lambda-core-model
terraform init && terraform apply

# ~4 min: connectors settle from PENDING to ACTIVE before apply returns
terraform output trusted_connector_arn
terraform output server_url
```

Point the worker stack at one of the connectors and let the reconciler pick it up:

```bash
cd ../devin-outpost-microvm
terraform apply -var="egress_connector_arn=$(terraform -chdir=../devin-outpost-vpc-demo output -raw trusted_connector_arn)"
```

Then start a Devin session on the `aws-lambda-microvm` platform and have it fetch
`server_url`. Swap in `restricted_connector_arn` and repeat to see the same
request time out while internet egress keeps working.

Observed results, one real session per connector:

```text
# trusted
internal OK internal-service-reachable
internet OK 3.148.55.53          # the NAT gateway's EIP

# restricted
internal FAIL URLError <urlopen error timed out>
internet OK 3.148.55.53
```

The worker sees a link-local `169.254.0.2` address rather than a VPC address —
Lambda NATs MicroVM traffic onto the connector's ENIs, so subnet and security
group rules still apply even though the guest never holds a VPC IP.

## Teardown

```bash
cd ../devin-outpost-microvm && terraform apply   # drop egress_connector_arn first
cd ../devin-outpost-vpc-demo && terraform destroy
```

Release the connectors before destroying, or deletion fails while a MicroVM is
still attached. The NAT gateway is the meaningful cost here (~$0.045/hr plus data
processing), so destroy the stack when the demo is done.
