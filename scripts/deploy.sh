#!/bin/bash
# Bird Observatory — Deploy
# Runs verification, restarts services, syncs dashboard to NAS.
set -eu

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

echo "Bird Observatory — Deploy"
echo "========================="

# Step 1: Verify
echo ""
echo "Step 1: Verification"
if ! "$SCRIPT_DIR/verify.sh"; then
    echo ""
    echo "❌ Verification failed. Fix issues before deploying."
    exit 1
fi

# Step 2: Restart services
echo ""
echo "Step 2: Restarting services..."
UID_NUM=$(id -u)
for svc in bird-audio bird-enhanced-audio bird-classifier bird-dashboard; do
    launchctl kickstart -k "gui/${UID_NUM}/com.vives.${svc}" 2>/dev/null && \
        echo "  Restarted $svc" || echo "  ⚠ Failed to restart $svc"
done

# Step 3: Sync dashboard to NAS
echo ""
echo "Step 3: Syncing dashboard to NAS..."
NAS_HOST="vives@192.168.5.92"
NAS_PORT=2000
SSH_KEY="/Users/vives/.ssh/id_ed25519"
SSH_OPTS="-i ${SSH_KEY} -o ConnectTimeout=10 -o BatchMode=yes"
NAS_DIR="/volume1/docker/birds-hls"

scp -P ${NAS_PORT} ${SSH_OPTS} \
    "${BASE_DIR}/dashboard/index.html" \
    "${NAS_HOST}:${NAS_DIR}/index.html" 2>/dev/null && \
    echo "  Synced index.html" || echo "  ⚠ Failed to sync index.html"

scp -P ${NAS_PORT} ${SSH_OPTS} \
    "${BASE_DIR}/dashboard/docs.html" \
    "${NAS_HOST}:${NAS_DIR}/docs.html" 2>/dev/null && \
    echo "  Synced docs.html" || echo "  ⚠ Failed to sync docs.html"

# Step 4: Quick health check
echo ""
echo "Step 4: Post-deploy health check (10s)..."
sleep 10
/usr/bin/python3 "${BASE_DIR}/health_monitor.py" 2>&1 | grep -E "INFO|ERROR|WARNING"

echo ""
echo "✅ Deploy complete"
