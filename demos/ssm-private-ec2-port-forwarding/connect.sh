#!/usr/bin/env bash
################################################################################
# SSH into the private instance THROUGH an SSM port-forwarding session.
#
# How it works:
#   1. `aws ssm start-session --document-name AWS-StartPortForwardingSession`
#      opens a local TCP port (default 2222) that is tunneled — over the SSM
#      agent's outbound connection — to port 22 on the instance. No inbound
#      security-group rule and no public IP are involved.
#   2. We then run `ssh` against 127.0.0.1:<localport>, which travels back down
#      that same reverse tunnel to sshd on the instance.
#
# Usage:
#   ./connect.sh                     # opens an interactive SSH shell
#   ./connect.sh 'uname -a && id'    # runs a one-off remote command and exits
#
# Env overrides: LOCAL_PORT (default 2222), SSH_USER (default ec2-user)
#
# Requires the AWS CLI Session Manager plugin:
#   https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-install-plugin.html
################################################################################

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_FILE="$SCRIPT_DIR/.demo-state"

if [ ! -f "$STATE_FILE" ]; then
  echo "No state file found ($STATE_FILE). Run ./provision.sh first." >&2
  exit 1
fi
# shellcheck disable=SC1090
source "$STATE_FILE"
export AWS_DEFAULT_REGION="$REGION"

LOCAL_PORT="${LOCAL_PORT:-2222}"
SSH_USER="${SSH_USER:-ec2-user}"

echo "Opening SSM port-forward: localhost:$LOCAL_PORT -> $INSTANCE:22 ..."
aws ssm start-session --target "$INSTANCE" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "{\"portNumber\":[\"22\"],\"localPortNumber\":[\"$LOCAL_PORT\"]}" \
  >"/tmp/ssm-pf-$INSTANCE.log" 2>&1 &
PF_PID=$!
cleanup() { kill "$PF_PID" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Wait for the local port to accept connections
for _ in $(seq 1 20); do
  if (exec 3<>"/dev/tcp/127.0.0.1/$LOCAL_PORT") 2>/dev/null; then exec 3>&- 3<&-; break; fi
  sleep 1
done

SSH_OPTS=(-i "$KEY_PATH" -p "$LOCAL_PORT"
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=15 -o LogLevel=ERROR)

if [ "$#" -gt 0 ]; then
  # shellcheck disable=SC2029  # remote-side command expansion is intended
  ssh "${SSH_OPTS[@]}" "$SSH_USER@127.0.0.1" "$@"
else
  # shellcheck disable=SC2029
  ssh "${SSH_OPTS[@]}" "$SSH_USER@127.0.0.1"
fi
