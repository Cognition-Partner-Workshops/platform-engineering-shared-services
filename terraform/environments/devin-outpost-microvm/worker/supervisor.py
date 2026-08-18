"""Lifecycle-hook supervisor for a Devin Outpost worker in a Lambda MicroVM.

Lambda snapshots this process during image build and resumes one MicroVM per
Devin session from that snapshot. The worker itself must therefore *not* be
running at snapshot time: everything unique to a session (the connect token,
the session ID) arrives later, in the /run hook payload.

The worker runs in the CLI's direct-serve mode, the same mode devin-outpost-k8s
uses for its worker pods: given `DEVIN_REMOTE_SESSION_TOKEN` it serves exactly
the one session named by `--session` and never contacts the queue API. Claim,
renewal and release stay with the reconciler, so this MicroVM holds no account
credentials -- only a token scoped to its own session.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1"
HOOK_PORT = int(os.environ.get("HOOK_PORT", "8080"))

# Lambda kills the build if a hook holds the connection open past its timeout,
# so every handler here answers immediately and does its work in the background.
worker = None
worker_lock = threading.Lock()

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
log = logging.getLogger("supervisor")


class Worker:
    """A single `devin worker start --session <id>` child process."""

    def __init__(self, session_id, env):
        self.session_id = session_id
        self.process = subprocess.Popen(
            ["devin", "worker", "start", "--session", session_id],
            env=env,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )

    def status(self):
        code = self.process.poll()
        return {
            "session_id": self.session_id,
            "pid": self.process.pid,
            "running": code is None,
            "exit_code": code,
        }

    def stop(self, timeout=30):
        if self.process.poll() is not None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()


def start_worker(payload):
    """Launch the worker for the session described by the /run hook payload.

    Returns an error string, or None on success.
    """
    session_id = payload.get("session_id")
    connect_token = payload.get("connect_token")
    if not session_id or not connect_token:
        return "run hook payload requires session_id and connect_token"

    env = os.environ.copy()
    env["DEVIN_REMOTE_SESSION_TOKEN"] = connect_token
    for key, name in (
        ("gateway_url", "DEVIN_OUTPOST_GATEWAY_URL"),
        ("remote_binary_sha", "DEVIN_WORKER_REMOTE_SHA"),
        ("api_url", "DEVIN_API_URL"),
    ):
        if payload.get(key):
            env[name] = payload[key]

    global worker
    with worker_lock:
        if worker is not None and worker.status()["running"]:
            return None if worker.session_id == session_id else "worker already serving another session"
        os.makedirs(env.get("DEVIN_WORKER_CACHE_DIR", "/var/lib/devin-worker/cache"), exist_ok=True)
        worker = Worker(session_id, env)
    log.info("started worker for session %s", session_id)
    return None


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _respond(self, status, body=None):
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):
        if self.path.rstrip("/") in ("/health", "/"):
            with worker_lock:
                status = worker.status() if worker else {"running": False}
            return self._respond(200, status)
        self._respond(404, {"error": "not found"})

    def do_POST(self):
        hook = self.path.rstrip("/")
        if not hook.startswith(HOOK_PREFIX):
            return self._respond(404, {"error": "not found"})
        hook = hook[len(HOOK_PREFIX):].lstrip("/")

        # /ready runs during image build, before any session exists. Answering
        # 200 here is what tells Lambda the snapshot is safe to take.
        if hook in ("ready", "validate"):
            return self._respond(200, {"ok": True})

        if hook == "run":
            body = self._read_json()
            raw = body.get("runHookPayload") or "{}"
            try:
                payload = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                return self._respond(400, {"error": "runHookPayload is not valid JSON"})
            error = start_worker(payload)
            if error:
                log.error("run hook rejected: %s", error)
                return self._respond(400, {"error": error})
            return self._respond(200, {"ok": True})

        # Suspend and resume preserve memory and disk, so the worker process
        # survives across the boundary and needs nothing done to it here.
        if hook in ("suspend", "resume"):
            return self._respond(200, {"ok": True})

        if hook == "terminate":
            with worker_lock:
                if worker:
                    worker.stop()
            return self._respond(200, {"ok": True})

        self._respond(404, {"error": "unknown hook"})


def main():
    server = ThreadingHTTPServer(("0.0.0.0", HOOK_PORT), Handler)
    log.info("supervisor listening on %d", HOOK_PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
