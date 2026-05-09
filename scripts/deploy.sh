#!/bin/bash
# Bird Observatory — Deploy
# Runs verify.sh, restarts the bird-* LaunchAgents via launchctl kickstart,
# checks go2rtc reachability at :1984, and runs health_monitor.py once.
# Does not sync to a NAS — the NAS path was retired in March 2026 and the
# observatory now runs entirely on the iMac.
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

# Step 3: Verify go2rtc is running
echo ""
echo "Step 3: Checking go2rtc..."
curl -s http://localhost:1984/api/streams > /dev/null 2>&1 && \
    echo "  go2rtc OK" || echo "  ⚠ go2rtc not responding"

# Step 4: Quick health check
echo ""
echo "Step 4: Post-deploy health check (10s)..."
sleep 10
/usr/bin/python3 "${BASE_DIR}/health_monitor.py" 2>&1 | grep -E "INFO|ERROR|WARNING"

echo ""
echo "✅ Deploy complete"
