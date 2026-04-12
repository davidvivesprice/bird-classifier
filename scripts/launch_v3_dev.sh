#!/bin/bash
# Launch the v3 dev stack: substream config, v3 pipeline, dev dashboard.
#
# Usage:
#   ./scripts/launch_v3_dev.sh
#
# Opens on http://127.0.0.1:8199/ — use a real browser (not headless) to
# verify smooth HD video + floating labels + sync delay. Uses the existing
# worktree models symlinks.
#
# Run ./scripts/stop_v3_dev.sh when done to tear down and restart v2.

set -e

WORKTREE="/Users/vives/bird-classifier/.worktrees/pipeline-v3"
VENV_CORAL="/Users/vives/bird-classifier/venv-coral/bin/python"
VENV_DASHBOARD="/Users/vives/bird-classifier/venv/bin/uvicorn"
V2_PLIST="$HOME/Library/LaunchAgents/com.vives.bird-pipeline.plist"

cd "$WORKTREE"

echo "[1/5] Adding go2rtc substreams via runtime API..."
curl -s -X PUT "http://127.0.0.1:1984/api/streams?name=feeder-sub&src=ffmpeg:feeder-main%23video%3Dh264%23width%3D640%23height%3D360" >/dev/null 2>&1 || true
curl -s -X PUT "http://127.0.0.1:1984/api/streams?name=ground-sub&src=ffmpeg:ground-main%23video%3Dh264%23width%3D640%23height%3D360" >/dev/null 2>&1 || true
STREAMS=$(curl -s http://127.0.0.1:1984/api/streams | python3 -c "import sys, json; print(','.join(sorted(json.load(sys.stdin).keys())))")
echo "    streams: $STREAMS"

echo "[2/5] Stopping v2 LaunchAgent to free Coral..."
launchctl unload "$V2_PLIST" 2>/dev/null || true
sleep 3

echo "[3/5] Starting v3 pipeline in worktree..."
PIPELINE_HEALTH_PORT=8102 \
PIPELINE_SSE_PORT=8104 \
"$VENV_CORAL" -u bird_pipeline_v3.py > /tmp/v3-run.log 2>&1 &
V3_PID=$!
echo "    v3 pid: $V3_PID (log: /tmp/v3-run.log)"

echo "[4/5] Waiting for v3 health endpoint to respond..."
for i in $(seq 1 30); do
  if curl -s -m 2 http://127.0.0.1:8102/api/pipeline/health >/dev/null 2>&1; then
    echo "    v3 health up after ${i}s"
    break
  fi
  sleep 1
done

echo "[5/5] Starting dev dashboard on :8199..."
PIPELINE_BACKEND_URL=http://127.0.0.1:8104 \
"$VENV_DASHBOARD" dashboard.api:app --host 127.0.0.1 --port 8199 > /tmp/v3-dash.log 2>&1 &
DASH_PID=$!
echo "    dashboard pid: $DASH_PID (log: /tmp/v3-dash.log)"

# Save pids for stop_v3_dev.sh
echo "$V3_PID $DASH_PID" > /tmp/v3-dev.pids

sleep 5
echo ""
echo "✓ v3 dev stack running"
echo ""
echo "Open in a real browser:"
echo "    http://127.0.0.1:8199/"
echo ""
echo "Tune the sync delay via browser console:"
echo "    localStorage.setItem('v3SyncDelayMs', '2000'); location.reload()"
echo ""
echo "When done:  ./scripts/stop_v3_dev.sh"
