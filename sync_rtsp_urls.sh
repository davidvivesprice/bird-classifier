#!/bin/bash
# Sync RTSP URLs from NAS after the nightly token refresh (runs at 3:05 AM).
# This script runs at 3:10 AM via LaunchAgent, pulls the updated rtsp_urls.json,
# and restarts the audio services so they pick up the new tokens.
set -eu

REMOTE_HOST="vives@192.168.5.92"
REMOTE_PORT=2000
SSH_KEY="/Users/vives/.ssh/id_ed25519"
SSH_OPTS="-p ${REMOTE_PORT} -i ${SSH_KEY} -o ConnectTimeout=10 -o BatchMode=yes"
REMOTE_FILE="/volume1/docker/birds-hls/rtsp_urls.json"
LOCAL_FILE="/Users/vives/bird-classifier/rtsp_urls.json"

# Fetch the file
scp ${SSH_OPTS} "${REMOTE_HOST}:${REMOTE_FILE}" "${LOCAL_FILE}.tmp" 2>/dev/null || {
    echo "$(date '+%Y-%m-%d %H:%M:%S') Failed to fetch rtsp_urls.json from NAS"
    exit 1
}

# Validate JSON
if ! python3 -c "import json; json.load(open('${LOCAL_FILE}.tmp'))" 2>/dev/null; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') Invalid JSON in rtsp_urls.json"
    rm -f "${LOCAL_FILE}.tmp"
    exit 1
fi

# Atomic replace
mv "${LOCAL_FILE}.tmp" "${LOCAL_FILE}"
echo "$(date '+%Y-%m-%d %H:%M:%S') Updated rtsp_urls.json"

# Restart audio services so they reconnect with fresh tokens
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-enhanced-audio" 2>/dev/null || true
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-audio" 2>/dev/null || true
echo "$(date '+%Y-%m-%d %H:%M:%S') Restarted audio services"
