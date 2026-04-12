#!/bin/bash
# Tear down the v3 dev stack launched by launch_v3_dev.sh and restore v2.

set -e

V2_PLIST="$HOME/Library/LaunchAgents/com.vives.bird-pipeline.plist"

if [ -f /tmp/v3-dev.pids ]; then
  read V3_PID DASH_PID < /tmp/v3-dev.pids
  echo "Killing v3 pids: $V3_PID $DASH_PID"
  kill -TERM "$V3_PID" "$DASH_PID" 2>/dev/null || true
  sleep 2
  kill -KILL "$V3_PID" "$DASH_PID" 2>/dev/null || true
  rm -f /tmp/v3-dev.pids
else
  echo "No /tmp/v3-dev.pids file — looking for v3 processes manually..."
  pkill -f bird_pipeline_v3.py 2>/dev/null || true
  pkill -f "uvicorn.*8199" 2>/dev/null || true
  sleep 2
fi

echo "Removing runtime-added substreams from go2rtc..."
curl -s -X DELETE "http://127.0.0.1:1984/api/streams?src=feeder-sub" >/dev/null 2>&1 || true
curl -s -X DELETE "http://127.0.0.1:1984/api/streams?src=ground-sub" >/dev/null 2>&1 || true

echo "Restarting v2 LaunchAgent..."
launchctl load "$V2_PLIST" 2>/dev/null || true
sleep 5

V2_HEALTH=$(curl -s -m 5 http://127.0.0.1:8100/api/pipeline/health 2>/dev/null | python3 -c "import sys, json; print(json.load(sys.stdin).get('overall', 'unknown'))" 2>/dev/null || echo "unreachable")
echo "v2 health: $V2_HEALTH"
echo "Done."
