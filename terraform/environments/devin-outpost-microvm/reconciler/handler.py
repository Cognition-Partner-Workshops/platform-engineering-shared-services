"""Reconciles the Devin Outposts queue onto Lambda MicroVM workers.

This is the MicroVM counterpart of the operator in
https://github.com/CognitionAI/devin-outpost-k8s: it owns the account token and
performs every queue operation (list, claim, renew, release), then runs one
MicroVM per claimed session in place of the operator's one worker Pod per
claimed session. Workers run in direct-serve mode and hold no account
credentials -- only the per-session connect token, handed to them through the
/run hook payload.

The operator drives its loop from the API's SSE watch stream. Lambda caps an
invocation at 15 minutes, so this runs as a periodic poll instead. Claims last
about 5 minutes, so the schedule interval must stay well under the renew margin.

`plan()` is kept free of I/O for the same reason the operator keeps `plan.rs`
pure: the whole session-to-MicroVM lifecycle mapping is then testable without a
queue server or an AWS account.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import boto3

log = logging.getLogger()
log.setLevel(logging.INFO)

API_VERSION_PREFIX = "/opbeta/outposts"
REQUEST_TIMEOUT = 30


class OutpostsClient:
    """Client for the `/opbeta/outposts` queue API, bound to one account token."""

    def __init__(self, base_url, token, acceptor_id):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.acceptor_id = acceptor_id

    def _request(self, method, path, query=None, body=None):
        url = f"{self.base_url}{API_VERSION_PREFIX}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", f"Bearer {self.token}")
        request.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
            payload = response.read()
        return json.loads(payload) if payload else {}

    def list_all(self, outpost_id):
        """Every queue item for the outpost, following pagination.

        Paging is at-least-once, so a row can repeat on a page boundary; the
        later page carries the fresher copy and wins.
        """
        items = {}
        cursor = None
        while True:
            query = {"outpost": outpost_id}
            if cursor:
                query["cursor"] = cursor
            page = self._request("GET", "/devins", query=query)
            for item in page.get("items", []):
                items[item["metadata"]["session_id"]] = item
            if not page.get("has_next_page"):
                return list(items.values())
            cursor = page.get("cursor")

    def claim(self, session_id):
        """Claim, or renew if already held by this acceptor. 409 means lost."""
        return self._request(
            "POST", f"/devins/{session_id}/claim", body={"acceptor_id": self.acceptor_id}
        )

    def release(self, session_id):
        return self._request(
            "POST", f"/devins/{session_id}/release", body={"acceptor_id": self.acceptor_id}
        )


def is_conflict(error):
    """A 409 from claim/release: the claim expired or moved to another worker."""
    return isinstance(error, urllib.error.HTTPError) and error.code == 409


def plan(sessions, records, acceptor_id, max_concurrent, renew_margin, now):
    """Map observed queue + MicroVM state onto the actions to take.

    Actions are independent and applied best-effort; whatever fails converges on
    the next pass.
    """
    def claimed_by_us(session):
        status = session["status"]
        return status.get("phase") == "claimed" and status.get("acceptor_id") == acceptor_id

    actions = []
    # A session on its way out is not occupying a worker slot, so it must not
    # hold capacity away from the next pending session.
    held = [
        s
        for s in sessions
        if claimed_by_us(s) and s["status"].get("session_status") not in ("terminated", "suspended")
    ]
    capacity = max(0, max_concurrent - len(held))
    handled = set()

    for session in sessions:
        session_id = session["metadata"]["session_id"]
        status = session["status"]
        record = records.get(session_id)

        if status.get("session_status") == "terminated":
            handled.add(session_id)
            actions.append(("terminate", session_id, None))
            continue

        # A suspended session gives its MicroVM back rather than holding capacity.
        # The queue re-enqueues it as a `resume` item, which is served by a fresh
        # MicroVM, matching the operator's NoSnapshot resume policy.
        if status.get("session_status") == "suspended":
            handled.add(session_id)
            if claimed_by_us(session) or record:
                actions.append(("suspend", session_id, None))
            continue

        if claimed_by_us(session):
            handled.add(session_id)
            if record is None or not record.get("microvm_id"):
                # Fresh claim, or the reconciler restarted before it could run a
                # MicroVM. Re-claiming yields a new connect token for the worker.
                actions.append(("start_worker", session_id, None))
            else:
                deadline = status.get("claim_deadline")
                if deadline is not None and deadline - now < renew_margin:
                    actions.append(("renew", session_id, None))
        elif status.get("phase") == "pending" and capacity > 0:
            capacity -= 1
            actions.append(("claim", session_id, None))

    for session_id, record in records.items():
        if session_id not in handled:
            actions.append(("release_orphan", session_id, record.get("microvm_id")))

    return actions


class Reconciler:
    def __init__(self):
        self.region = os.environ["AWS_REGION"]
        self.outpost_id = os.environ["OUTPOST_ID"]
        self.image_arn = os.environ["MICROVM_IMAGE_ARN"]
        self.table_name = os.environ["STATE_TABLE"]
        self.execution_role_arn = os.environ["MICROVM_EXECUTION_ROLE_ARN"]
        self.log_group = os.environ["MICROVM_LOG_GROUP"]
        self.acceptor_id = os.environ["ACCEPTOR_ID"]
        self.max_concurrent = int(os.environ.get("MAX_CONCURRENT_SESSIONS", "2"))
        self.renew_margin = int(os.environ.get("CLAIM_RENEW_MARGIN_SECONDS", "60"))
        self.max_duration = int(os.environ.get("MICROVM_MAX_DURATION_SECONDS", "28800"))
        self.record_ttl = int(os.environ.get("STATE_RECORD_TTL_SECONDS", "86400"))
        self.ingress_connectors = _split(os.environ.get("INGRESS_CONNECTORS", ""))
        self.egress_connectors = _split(os.environ.get("EGRESS_CONNECTORS", ""))

        self.microvms = boto3.client("lambda-microvms", region_name=self.region)
        self.table = boto3.resource("dynamodb", region_name=self.region).Table(self.table_name)
        token = boto3.client("secretsmanager", region_name=self.region).get_secret_value(
            SecretId=os.environ["OUTPOST_TOKEN_SECRET_ARN"]
        )["SecretString"]
        self.client = OutpostsClient(
            os.environ.get("DEVIN_API_URL", "https://api.devin.ai"), token, self.acceptor_id
        )

    def load_records(self):
        records = {}
        kwargs = {}
        while True:
            page = self.table.scan(**kwargs)
            for item in page.get("Items", []):
                records[item["session_id"]] = item
            if "LastEvaluatedKey" not in page:
                return records
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def run(self):
        sessions = self.client.list_all(self.outpost_id)
        records = self.load_records()
        now = int(time.time())
        actions = plan(
            sessions, records, self.acceptor_id, self.max_concurrent, self.renew_margin, now
        )
        log.info("%d sessions, %d tracked, actions=%s", len(sessions), len(records), actions)

        by_id = {s["metadata"]["session_id"]: s for s in sessions}
        for action, session_id, microvm_id in actions:
            try:
                self.apply(action, session_id, microvm_id, by_id.get(session_id), records)
            except Exception:
                # One failed session must not stall the rest of the queue.
                log.exception("action %s failed for %s", action, session_id)
        self.terminate_untracked()
        return {"sessions": len(sessions), "actions": len(actions)}

    def apply(self, action, session_id, microvm_id, session, records):
        if action in ("claim", "start_worker"):
            self.start_worker(session_id)
        elif action == "renew":
            self.client.claim(session_id)
            log.info("renewed claim on %s", session_id)
        elif action in ("terminate", "suspend", "release_orphan"):
            self.tear_down(session_id, microvm_id or (records.get(session_id) or {}).get("microvm_id"))

    def start_worker(self, session_id):
        """Claim the session and run a MicroVM to serve it.

        The claim response is the only place the connect token and gateway URL
        exist, so the MicroVM has to be started from the same claim that
        produced them.
        """
        try:
            claimed = self.client.claim(session_id)
        except urllib.error.HTTPError as error:
            if is_conflict(error):
                log.info("claim on %s lost to another worker", session_id)
                return
            raise

        status = claimed["status"]
        payload = {
            "session_id": session_id,
            "connect_token": status.get("connect_token"),
            "gateway_url": status.get("gateway_url"),
            "remote_binary_sha": claimed.get("spec", {}).get("remote_binary_sha"),
        }
        if not payload["connect_token"]:
            log.error("claim on %s returned no connect token", session_id)
            return

        request = {
            "imageIdentifier": self.image_arn,
            "executionRoleArn": self.execution_role_arn,
            "runHookPayload": json.dumps({k: v for k, v in payload.items() if v}),
            "maximumDurationInSeconds": self.max_duration,
            "logging": {"cloudWatch": {"logGroup": self.log_group, "logStream": session_id}},
            "clientToken": self.reserve_attempt(session_id),
        }
        if self.ingress_connectors:
            request["ingressNetworkConnectors"] = self.ingress_connectors
        if self.egress_connectors:
            request["egressNetworkConnectors"] = self.egress_connectors
        # No idlePolicy: idleness is measured as absence of traffic on the
        # MicroVM's endpoint, and a Devin worker talks outbound to the gateway
        # only. With a policy set it would be suspended mid-session.

        microvm = self.microvms.run_microvm(**request)
        self.table.update_item(
            Key={"session_id": session_id},
            UpdateExpression="SET microvm_id = :m, updated_at = :n",
            ExpressionAttributeValues={":m": microvm["microvmId"], ":n": int(time.time())},
        )
        log.info("session %s -> microvm %s", session_id, microvm["microvmId"])

    def reserve_attempt(self, session_id):
        """The RunMicrovm idempotency token for this session's current launch.

        Written before RunMicrovm so that retrying a call that timed out reuses
        the MicroVM it already started rather than stranding a second one. It has
        to be per-launch rather than per-session: a session gets a fresh MicroVM
        after a suspend or a lost claim, and reusing the token there would hand
        back the terminated one. `tear_down` drops the record, so the next launch
        reserves a new token.
        """
        now = int(time.time())
        record = self.table.update_item(
            Key={"session_id": session_id},
            UpdateExpression=(
                "SET attempt_token = if_not_exists(attempt_token, :t), "
                "updated_at = :n, expires_at = :e"
            ),
            ExpressionAttributeValues={
                ":t": uuid.uuid4().hex,
                ":n": now,
                ":e": now + self.record_ttl,
            },
            ReturnValues="ALL_NEW",
        )["Attributes"]
        return record["attempt_token"]

    def tear_down(self, session_id, microvm_id):
        if microvm_id:
            try:
                self.microvms.terminate_microvm(microvmIdentifier=microvm_id)
                log.info("terminated microvm %s for session %s", microvm_id, session_id)
            except self.microvms.exceptions.ResourceNotFoundException:
                pass
        try:
            self.client.release(session_id)
        except urllib.error.HTTPError as error:
            # 409: the claim already moved on. 404/410: the session is gone from
            # the queue. Either way the record has nothing left to track, and
            # keeping it would replay this teardown every pass until its TTL.
            if not is_conflict(error) and error.code not in (404, 410):
                raise
        finally:
            self.table.delete_item(Key={"session_id": session_id})

    def terminate_untracked(self):
        """Terminate MicroVMs from our image that no record points at.

        Covers a reconciler crash between `run_microvm` and the state write,
        which would otherwise leave a MicroVM billing until its maximum
        duration elapsed.
        """
        tracked = {r.get("microvm_id") for r in self.load_records().values()}
        paginator = {"imageIdentifier": self.image_arn}
        while True:
            page = self.microvms.list_microvms(**paginator)
            for item in page.get("items", []):
                if item["microvmId"] in tracked:
                    continue
                if item.get("state") in ("TERMINATED", "TERMINATING"):
                    continue
                log.warning("terminating untracked microvm %s", item["microvmId"])
                self.microvms.terminate_microvm(microvmIdentifier=item["microvmId"])
            if not page.get("nextToken"):
                return
            paginator["nextToken"] = page["nextToken"]


def _split(value):
    return [part.strip() for part in value.split(",") if part.strip()]


def handler(event, context):
    return Reconciler().run()
