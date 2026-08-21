#!/usr/bin/env python3
"""Create, look up or delete a Lambda VPC egress network connector.

The Terraform AWS provider has no network connector resource yet
(hashicorp/terraform-provider-aws#48526), so the connector is managed by this
script from a `terraform_data` provisioner, with `lookup` feeding its ARN back
to Terraform through an `external` data source.
"""

import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError


def find(client, name):
    paginator = {}
    while True:
        page = client.list_network_connectors(**paginator)
        for item in page["NetworkConnectors"]:
            if item["Name"] == name:
                return item
        if not page.get("NextMarker"):
            return None
        paginator["Marker"] = page["NextMarker"]


def wait_for(client, arn, states, timeout, poll=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            state = client.get_network_connector(Identifier=arn)["State"]
        except ClientError as error:
            if error.response["Error"]["Code"] == "ResourceNotFoundException":
                return None
            raise
        if state in states:
            return state
        if state in ("FAILED", "DELETE_FAILED"):
            raise SystemExit(f"connector {arn} entered {state}")
        print(f"  connector state={state}", flush=True)
        time.sleep(poll)
    raise SystemExit(f"timed out after {timeout}s waiting for {arn}")


def create(args, client):
    configuration = {
        "VpcEgressConfiguration": {
            "SubnetIds": args.subnet_ids,
            "SecurityGroupIds": args.security_group_ids,
            "NetworkProtocol": "IPv4",
            "AssociatedComputeResourceTypes": ["MicroVm"],
        }
    }

    existing = find(client, args.name)
    if existing is None:
        print(f"creating network connector {args.name}", flush=True)
        arn = client.create_network_connector(
            Name=args.name,
            Configuration=configuration,
            OperatorRole=args.operator_role_arn,
        )["Arn"]
    else:
        # Subnets and security groups are the whole point of the connector, so
        # a change to either has to reach an existing one rather than be ignored.
        arn = existing["Arn"]
        print(f"updating network connector {args.name}", flush=True)
        client.update_network_connector(Identifier=arn, Configuration=configuration)

    wait_for(client, arn, {"ACTIVE"}, args.timeout)
    print(json.dumps({"arn": arn}))


def lookup(args, client):
    existing = find(client, args.name)
    print(json.dumps({"arn": existing["Arn"] if existing else ""}))


def delete(args, client):
    existing = find(client, args.name)
    if existing is None:
        print(f"network connector {args.name} already gone")
        return

    # A connector with MicroVMs still attached is rejected until they drain, so
    # deletion retries rather than failing the destroy.
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        try:
            client.delete_network_connector(Identifier=existing["Arn"])
            wait_for(client, existing["Arn"], {"DELETING"}, args.timeout)
            print(f"deleted network connector {args.name}")
            return
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                return
            if code not in ("ConflictException", "ResourceConflictException"):
                raise
            print(f"  waiting to delete connector ({error})", flush=True)
            time.sleep(15)
    raise SystemExit(f"timed out after {args.timeout}s deleting {args.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "lookup", "delete"])
    parser.add_argument("--region", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--subnet-ids", nargs="+", default=[])
    parser.add_argument("--security-group-ids", nargs="+", default=[])
    parser.add_argument("--operator-role-arn")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()

    try:
        client = boto3.client("lambda-core", region_name=args.region)
    except Exception:
        raise SystemExit(
            "boto3 has no lambda-core client. Upgrade with "
            "`pip install -U 'boto3>=1.41' 'botocore>=1.43'`."
        )

    if args.action == "create":
        for required in ("subnet_ids", "security_group_ids", "operator_role_arn"):
            if not getattr(args, required):
                parser.error(f"--{required.replace('_', '-')} is required for create")
        create(args, client)
    elif args.action == "lookup":
        lookup(args, client)
    else:
        delete(args, client)


if __name__ == "__main__":
    sys.exit(main())
