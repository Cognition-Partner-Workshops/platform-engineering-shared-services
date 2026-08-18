#!/usr/bin/env python3
"""Create, update or delete a Lambda MicroVM image.

The Terraform AWS provider has no MicroVM resources yet
(hashicorp/terraform-provider-aws#48526), so the image is managed by this
script from a `terraform_data` provisioner. Keeping it idempotent -- create when
absent, new version when present -- is what lets Terraform own the lifecycle
anyway, including teardown.
"""

import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

TERMINAL_OK = {"CREATED", "UPDATED"}
TERMINAL_FAILED = {"CREATION_FAILED", "UPDATE_FAILED", "DELETION_FAILED"}


def wait_for_image(client, image_arn, timeout, poll=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        image = client.get_microvm_image(imageIdentifier=image_arn)
        state = image["state"]
        if state in TERMINAL_OK:
            return image
        if state in TERMINAL_FAILED:
            raise SystemExit(
                f"image {image_arn} entered {state}; check CloudWatch "
                f"/aws/lambda/microvms/{image['name']} for build logs"
            )
        print(f"  image state={state}", flush=True)
        time.sleep(poll)
    raise SystemExit(f"timed out after {timeout}s waiting for {image_arn}")


def get_image(client, image_arn):
    try:
        return client.get_microvm_image(imageIdentifier=image_arn)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("ResourceNotFoundException", "ValidationException"):
            return None
        raise


def create(args, client):
    build = {
        "baseImageArn": args.base_image_arn,
        "buildRoleArn": args.build_role_arn,
        "codeArtifact": {"uri": args.artifact_uri},
        "logging": {"cloudWatch": {"logGroup": args.log_group}},
        "resources": [{"minimumMemoryInMiB": args.memory_mib}],
        # Each hook is toggled on, not named: Lambda always POSTs to
        # /aws/lambda-microvms/runtime/v1/<hook> on the configured port.
        "hooks": {
            "port": args.hook_port,
            "microvmHooks": {
                "run": "ENABLED",
                "runTimeoutInSeconds": args.run_timeout,
                "suspend": "ENABLED",
                "resume": "ENABLED",
                "terminate": "ENABLED",
            },
            "microvmImageHooks": {
                "ready": "ENABLED",
                "readyTimeoutInSeconds": args.ready_timeout,
            },
        },
    }

    existing = get_image(client, args.image_arn)
    if existing is None:
        print(f"creating microvm image {args.name}", flush=True)
        client.create_microvm_image(name=args.name, **build)
    else:
        # update_microvm_image rejects the call unless the base image and build
        # role are repeated, even when only the artifact changed.
        print(f"updating microvm image {args.name} (state={existing['state']})", flush=True)
        client.update_microvm_image(imageIdentifier=args.image_arn, **build)

    image = wait_for_image(client, args.image_arn, args.timeout)
    print(json.dumps({"imageArn": image["imageArn"], "version": image.get("latestActiveImageVersion")}))


def live_microvms(client, image_arn):
    """MicroVMs from the image that are still billing or still shutting down."""
    live = []
    paginator = {"imageIdentifier": image_arn}
    while True:
        page = client.list_microvms(**paginator)
        live.extend(
            item for item in page.get("items", []) if item.get("state") != "TERMINATED"
        )
        if not page.get("nextToken"):
            return live
        paginator["nextToken"] = page["nextToken"]


def delete(args, client):
    if get_image(client, args.image_arn) is None:
        print(f"microvm image {args.name} already gone")
        return

    # Running MicroVMs keep billing and block image deletion, so drain first --
    # and DeleteMicrovmImage keeps rejecting until they reach TERMINATED, not
    # merely TERMINATING, so this waits the drain out rather than racing it.
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        live = live_microvms(client, args.image_arn)
        if not live:
            break
        for item in live:
            if item["state"] != "TERMINATING":
                print(f"terminating microvm {item['microvmId']}", flush=True)
                client.terminate_microvm(microvmIdentifier=item["microvmId"])
        print(f"  waiting for {len(live)} microvm(s) to terminate", flush=True)
        time.sleep(15)

    while time.time() < deadline:
        try:
            client.delete_microvm_image(imageIdentifier=args.image_arn)
            print(f"deleted microvm image {args.name}")
            return
        except ClientError as error:
            code = error.response["Error"]["Code"]
            if code == "ResourceNotFoundException":
                return
            message = error.response["Error"].get("Message", "")
            if code not in ("ConflictException", "ResourceConflictException") and (
                code != "ValidationException" or "running microvms" not in message
            ):
                raise
            print(f"  waiting to delete image ({message or code})", flush=True)
            time.sleep(15)
    raise SystemExit(f"timed out after {args.timeout}s deleting {args.image_arn}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["create", "delete"])
    parser.add_argument("--region", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--image-arn", required=True)
    parser.add_argument("--artifact-uri")
    parser.add_argument("--base-image-arn")
    parser.add_argument("--build-role-arn")
    parser.add_argument("--log-group")
    parser.add_argument("--memory-mib", type=int, default=2048)
    parser.add_argument("--hook-port", type=int, default=8080)
    parser.add_argument("--ready-timeout", type=int, default=600)
    # The service caps the run hook at 60s, so the hook must spawn the worker
    # and answer immediately rather than wait for it.
    parser.add_argument("--run-timeout", type=int, default=60)
    parser.add_argument("--timeout", type=int, default=2400)
    args = parser.parse_args()

    try:
        client = boto3.client("lambda-microvms", region_name=args.region)
    except Exception:
        raise SystemExit(
            "boto3 has no lambda-microvms client. Upgrade with "
            "`pip install -U 'boto3>=1.41' 'botocore>=1.43'`."
        )

    if args.action == "create":
        for required in ("artifact_uri", "base_image_arn", "build_role_arn", "log_group"):
            if not getattr(args, required):
                parser.error(f"--{required.replace('_', '-')} is required for create")
        create(args, client)
    else:
        delete(args, client)


if __name__ == "__main__":
    sys.exit(main())
