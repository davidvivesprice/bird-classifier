#!/usr/bin/env bash
# watch_tracker.sh — live verification of the tracker after a deploy.
# Samples the health payload's tracker block every 30 s for N minutes and
# greps the journal for v4 identity events. Run from the Mac or the Pi.
#   tools/watch_tracker.sh [minutes=10]
set -euo pipefail
MIN="${1:-10}"
PI="${LAB_PI_HOST:-vives@192.168.6.156}"
run() { if [ "$(hostname)" = "pi5" ]; then bash -c "$1"; else ssh -o ConnectTimeout=10 "$PI" "$1"; fi; }
echo "tracker watch: $MIN min, 30 s samples"
for i in $(seq 1 $((MIN * 2))); do
  run 'curl -s --max-time 4 http://127.0.0.1:8100/api/pipeline/health | python3 -c "
import json,sys
d=json.load(sys.stdin); f=d[\"pipeline\"].get(\"feeder\",{}); t=f.get(\"tracker\",{}); c=f.get(\"capture\",{})
print(f\"{d.get(\"uptime_s\",0):>6}s impl={t.get(\"impl\")} active={t.get(\"active_tracks\")} lost={t.get(\"lost_tracks\")} revivals={t.get(\"reid_revivals\")} merges={t.get(\"vote_merges\")} splits={t.get(\"vote_splits\")} switches={t.get(\"id_switches\")} frames={c.get(\"frames_processed\")} overall={d.get(\"overall\")}\")
" 2>/dev/null || echo "health unavailable"'
  sleep 30
done
echo "── journal: locks / unlocks / errors in the window ──"
run "journalctl --user -u bird-pipeline.service --since '-$((MIN + 1)) min' --no-pager 2>/dev/null | grep -cE 'LOCKED|UNLOCKED' | xargs -I{} echo 'lock/unlock lines: {}'; journalctl --user -u bird-pipeline.service --since '-$((MIN + 1)) min' --no-pager 2>/dev/null | grep -iE 'traceback|error|fatal' | grep -v 'GPU device discovery' | head -5"
