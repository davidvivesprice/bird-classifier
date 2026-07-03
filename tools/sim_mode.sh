#!/usr/bin/env bash
# sim_mode.sh — switch the pipeline between the simulated feeder camera and live.
#
#   tools/sim_mode.sh on    # pipeline reads the sim reel (feeder-demo) + records
#                           # every emitted event to /tmp/sim_events.jsonl
#   tools/sim_mode.sh off   # back to the live UniFi feeder
#   tools/sim_mode.sh grade # score the last sim run against the demo annotations
#
# In sim mode the pipeline processes the looping reel exactly like a live camera.
# Turn on "demo" in the dashboard to watch the same feed with the label overlay.
# The recorded /tmp/sim_events.jsonl is the complete, hang-proof trace of what
# the pipeline output frame-by-frame — feed it to the grade or share the path.
set -euo pipefail

DROPIN_DIR="$HOME/.config/systemd/user/bird-pipeline.service.d"
DROPIN="$DROPIN_DIR/demo.conf"
EVENTS=/tmp/sim_events.jsonl

case "${1:-}" in
  on)
    mkdir -p "$DROPIN_DIR"
    cat > "$DROPIN" <<EOF
[Service]
Environment=PIPELINE_TEST_RTSP_URL=rtsp://127.0.0.1:8554/feeder-demo
Environment=PIPELINE_RECORD_EVENTS=$EVENTS
Environment=PIPELINE_DISABLE_SEGMENTER=1
Environment=PIPELINE_DISABLE_SNAPSHOTS=1
EOF
    rm -f "$EVENTS"
    systemctl --user daemon-reload
    systemctl --user reset-failed bird-pipeline.service || true
    systemctl --user restart bird-pipeline.service
    sleep 8
    echo "SIM MODE ON — pipeline reading feeder-demo; recording -> $EVENTS"
    echo "active=$(systemctl --user is-active bird-pipeline.service)"
    ;;
  off)
    rm -f "$DROPIN"
    systemctl --user daemon-reload
    systemctl --user reset-failed bird-pipeline.service || true
    systemctl --user restart bird-pipeline.service
    sleep 8
    echo "SIM MODE OFF — pipeline back on the live feeder."
    echo "active=$(systemctl --user is-active bird-pipeline.service)"
    ;;
  grade)
    # Model-independent TRACKER metrics from the recorded events — works for any
    # clip (no annotations needed). For the annotated demo reel, run the full
    # classifier scorecard with: venv/bin/python3 tools/demo_grade.py ...
    python3 - "$EVENTS" <<'PY'
import sys, json
from collections import Counter
rows = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
def iou(a, b):
    ix1,iy1=max(a[0],b[0]),max(a[1],b[1]); ix2,iy2=min(a[2],b[2]),min(a[3],b[3])
    inter=max(0,ix2-ix1)*max(0,iy2-iy1)
    ua=(a[2]-a[0])*(a[3]-a[1])+(b[2]-b[0])*(b[3]-b[1])-inter
    return inter/ua if ua>0 else 0.0
ids = Counter()
life = {}   # track_id -> [first_pts, last_pts]
locks = Counter()
multi = dup = 0
for e in rows:
    ts = e.get("tracks", []); pts = e.get("pts", 0)
    if len(ts) >= 2:
        multi += 1
        if any(iou(ts[i]["bbox"], ts[j]["bbox"]) > 0.4
               for i in range(len(ts)) for j in range(i+1, len(ts))):
            dup += 1
    for t in ts:
        tid = t["track_id"]; ids[tid] += 1
        life.setdefault(tid, [pts, pts]); life[tid][1] = pts
        if t.get("is_locked") and t.get("species"):
            locks[t["species"]] += 1
durs = sorted((b-a) for a, b in life.values())
short = sum(1 for d in durs if d < 1.0)
print("================= TRACKER GRADE (model-independent) =================")
print(f"  events                 : {len(rows)}")
print(f"  distinct track IDs     : {len(ids)}")
print(f"  short-lived IDs (<1s)  : {short}/{len(ids)}   (fragmentation — high is bad)")
print(f"  median track lifetime  : {durs[len(durs)//2]:.1f}s" if durs else "  no tracks")
print(f"  duplicate-box rate     : {dup}/{multi} multi-track frames = {round(100*dup/max(multi,1))}%   (2 boxes on 1 bird — ideal 0%)")
print(f"  max simultaneous tracks: {max((len(e.get('tracks',[])) for e in rows), default=0)}")
if locks:
    print(f"  locked species (frames): {dict(locks.most_common())}")
print("=" * 68)
PY
    ;;
  *)
    echo "usage: $0 {on|off|grade}" >&2; exit 2;;
esac
