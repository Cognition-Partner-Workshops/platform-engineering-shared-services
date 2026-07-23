#!/usr/bin/env bash
#
# Bootstrap an Amazon EC2 Mac (Apple silicon) as a Devin Outpost worker.
# Run ON the Mac as the ec2-user (Amazon macOS AMIs ship Homebrew + Xcode CLT).
#
#   ssh -i devin-outpost-mac.pem ec2-user@<public-ip>
#   OUTPOST_NAME=<name> DEVIN_OUTPOSTS_TOKEN=<token> bash bootstrap-worker.sh
#
# Run it non-interactively (as above) so the CLI installer does not launch its
# interactive `devin setup` prompt (the worker authenticates via the token, not
# an interactive login). ec2-user needs passwordless sudo (default on the AMI).
#
# Optionally set REPOS="https://github.com/org/a.git https://github.com/org/b.git"
# to clone repositories into the worker's working directory.
#
# Persistence uses a LaunchDaemon (system domain) rather than a LaunchAgent
# because EC2 Macs are headless by default (no logged-in Aqua/GUI session, so
# `gui/<uid>` is unreachable). The daemon runs as ec2-user. NOTE: browser /
# computer-use and screen-recording need a logged-in window-server session —
# enable auto-login on the Mac if you need those.
set -euo pipefail

: "${OUTPOST_NAME:?Set OUTPOST_NAME to your outpost name}"
: "${DEVIN_OUTPOSTS_TOKEN:?Set DEVIN_OUTPOSTS_TOKEN to your outpost token}"
REPOS="${REPOS:-}"

WORK_DIR="${HOME}/devin-worker"
REPOS_DIR="${WORK_DIR}/repos"
LOG_DIR="${WORK_DIR}/logs"
mkdir -p "${REPOS_DIR}" "${LOG_DIR}"

echo "==> Ensuring Homebrew is on PATH"
if [ -x /opt/homebrew/bin/brew ]; then
  eval "$(/opt/homebrew/bin/brew shellenv)"
fi

echo "==> Verifying git"
git --version

echo "==> Installing ffmpeg (screen recording; optional but recommended)"
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg || echo "WARN: ffmpeg install failed; screen recording will be unavailable"

echo "==> Installing Google Chrome (browser / computer-use features; optional)"
if [ ! -d "/Applications/Google Chrome.app" ]; then
  brew install --cask google-chrome || echo "WARN: Chrome install failed; browser tools will be unavailable"
fi

echo "==> Installing the Devin CLI"
if ! command -v devin >/dev/null 2>&1 && [ ! -x "${HOME}/.local/bin/devin" ]; then
  curl -fsSL https://cli.devin.ai/install.sh | bash
else
  echo "    devin already installed; skipping"
fi

# Resolve the devin binary location for the LaunchDaemon (absolute path required).
DEVIN_BIN="$(command -v devin || true)"
if [ -z "${DEVIN_BIN}" ]; then
  for c in "${HOME}/.devin/bin/devin" "${HOME}/.local/bin/devin" /usr/local/bin/devin /opt/homebrew/bin/devin; do
    [ -x "$c" ] && DEVIN_BIN="$c" && break
  done
fi
: "${DEVIN_BIN:?Could not locate the devin binary after install}"
echo "==> Devin CLI at ${DEVIN_BIN}"

if [ -n "${REPOS}" ]; then
  echo "==> Cloning repositories into ${REPOS_DIR}"
  for r in ${REPOS}; do
    name="$(basename "${r}" .git)"
    [ -d "${REPOS_DIR}/${name}" ] || git -C "${REPOS_DIR}" clone "${r}"
  done
fi

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PLIST="/Library/LaunchDaemons/ai.devin.worker.plist"
TMP_PLIST="$(mktemp /tmp/ai.devin.worker.XXXX.plist)"

echo "==> Writing LaunchDaemon ${PLIST}"
cat > "${TMP_PLIST}" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>ai.devin.worker</string>
    <key>UserName</key><string>$(id -un)</string>
    <key>GroupName</key><string>staff</string>
    <key>ProgramArguments</key>
    <array>
        <string>${DEVIN_BIN}</string>
        <string>worker</string>
        <string>start</string>
        <string>--outpost=${OUTPOST_NAME}</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>DEVIN_OUTPOSTS_TOKEN</key><string>${DEVIN_OUTPOSTS_TOKEN}</string>
        <key>DEVIN_CHROME_PATH</key><string>${CHROME}</string>
        <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${HOME}/.local/bin</string>
        <key>HOME</key><string>${HOME}</string>
    </dict>
    <key>WorkingDirectory</key><string>${REPOS_DIR}</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>${LOG_DIR}/worker.out.log</string>
    <key>StandardErrorPath</key><string>${LOG_DIR}/worker.err.log</string>
</dict>
</plist>
PLIST_EOF

sudo cp "${TMP_PLIST}" "${PLIST}"
sudo chown root:wheel "${PLIST}"
sudo chmod 644 "${PLIST}"
rm -f "${TMP_PLIST}"

echo "==> (Re)loading the LaunchDaemon"
sudo launchctl bootout system/ai.devin.worker 2>/dev/null || true
sudo launchctl bootstrap system "${PLIST}"
sudo launchctl enable system/ai.devin.worker

echo "==> Done. Worker is running under launchd (label ai.devin.worker)."
echo "    Logs: ${LOG_DIR}/worker.{out,err}.log"
echo "    Status: sudo launchctl print system/ai.devin.worker | grep -E 'state|pid'"
