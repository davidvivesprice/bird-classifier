#!/usr/bin/env python3
"""Quantify David's three reported pathologies from the LIVE demo SSE capture:
1. lock-passing (locked track's box teleporting between birds)
2. duplicate boxes (two tracks overlapping on one bird)
3. 'identifying' dominance (unlocked fraction) + attempt-cap starvation proxy
Also: match-steal events (locked track dies while an unlocked track continues
on the same spot). Emits pathology moments (pts) for frame rendering."""
import json, sys
from collections import defaultdict

EV = sys.argv[1] if len(sys.argv) > 1 else "/tmp/lab_watch_events.jsonl"
LOOP = 149.933

events = []
for line in open(EV):
    line = line.strip()
    if not line.startswith("data:"): continue
    try:
        d = json.loads(line[5:])
    except Exception: continue
    if d.get("pts") is None: continue
    events.append(d)
events.sort(key=lambda e: (e["pts"], e.get("seq", 0)))
print(f"events: {len(events)}, pts {events[0]['pts']:.1f}..{events[-1]['pts']:.1f} "
      f"({events[-1]['pts']-events[0]['pts']:.0f}s of demo)")

def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter/ua if ua > 0 else 0.0
def containment(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    ma = min((a[2]-a[0])*(a[3]-a[1]), (b[2]-b[0])*(b[3]-b[1]))
    return inter/ma if ma > 0 else 0.0
def center(b): return ((b[0]+b[2])/2, (b[1]+b[3])/2)

# --- 1. identifying dominance ---
n_te = n_locked = 0
lock_species = defaultdict(int)
for e in events:
    for t in e["tracks"]:
        n_te += 1
        if t.get("is_locked"):
            n_locked += 1
            lock_species[t.get("species")] += 1
print(f"\n1. LABEL STATE: {n_te} track-events; locked {n_locked} "
      f"({100*n_locked/max(n_te,1):.1f}%) / identifying {100*(1-n_locked/max(n_te,1)):.1f}%")
print("   locked species distribution:", dict(lock_species))

# --- 2. duplicate boxes ---
dup_frames = 0
multi_frames = 0
dup_moments = []
for e in events:
    ts = e["tracks"]
    if len(ts) < 2: continue
    multi_frames += 1
    dup = False
    for i in range(len(ts)):
        for j in range(i+1, len(ts)):
            a, b = ts[i]["bbox"], ts[j]["bbox"]
            if iou(a, b) > 0.4 or containment(a, b) > 0.65:
                dup = True
                dup_moments.append((e["pts"], ts[i]["track_id"], ts[j]["track_id"],
                                    round(iou(a,b),2), round(containment(a,b),2)))
    if dup: dup_frames += 1
print(f"\n2. DUP BOXES: {dup_frames} frames with overlapping tracks "
      f"(of {multi_frames} multi-track frames = {100*dup_frames/max(multi_frames,1):.1f}%)")
for m in dup_moments[:5]: print("   e.g. pts=%.1f T%d~T%d iou=%.2f cont=%.2f" % m)

# --- 3. lock teleports (label passing) ---
last = {}   # tid -> (pts, center, species, locked)
teleports = []
for e in events:
    for t in e["tracks"]:
        tid = t["track_id"]
        c = center(t["bbox"])
        if tid in last:
            p0, c0, sp0, lk0 = last[tid]
            dt = e["pts"] - p0
            if 0 < dt <= 0.2 and t.get("is_locked") and lk0:
                jump = ((c[0]-c0[0])**2 + (c[1]-c0[1])**2) ** 0.5
                if jump > 120:   # >120px between consecutive events while LOCKED
                    teleports.append((e["pts"], tid, t.get("species"), round(jump), c0, c))
        last[tid] = (e["pts"], c, t.get("species"), t.get("is_locked"))
print(f"\n3. LOCKED-LABEL TELEPORTS (>120px jump while locked): {len(teleports)}")
for tp in teleports[:8]:
    print(f"   pts={tp[0]:.1f} T{tp[1]} '{tp[2]}' jumped {tp[3]}px {tuple(round(x) for x in tp[4])}->{tuple(round(x) for x in tp[5])}")

# --- 4. match-steal: locked track vanishes, unlocked track continues nearby ---
# build per-tid last-seen + first-seen
by_tid = defaultdict(list)
for e in events:
    for t in e["tracks"]:
        by_tid[t["track_id"]].append((e["pts"], t))
steals = []
for tid, seq in by_tid.items():
    seq.sort(key=lambda x: x[0])
    lastpts, lastt = seq[-1]
    if not lastt.get("is_locked"): continue
    if lastpts >= events[-1]["pts"] - 1: continue   # still alive at end
    lc = center(lastt["bbox"])
    # any OTHER unlocked track present within 1 body-width within 1s after death?
    w = lastt["bbox"][2] - lastt["bbox"][0]
    for tid2, seq2 in by_tid.items():
        if tid2 == tid: continue
        for p2, t2 in seq2:
            if lastpts < p2 <= lastpts + 1.0 and not t2.get("is_locked"):
                c2 = center(t2["bbox"])
                if ((c2[0]-lc[0])**2 + (c2[1]-lc[1])**2) ** 0.5 < max(w, 60):
                    steals.append((lastpts, tid, lastt.get("species"), tid2))
                    break
        else:
            continue
        break
print(f"\n4. LOCK DEATHS WITH NEARBY UNLOCKED SUCCESSOR (match-steal signature): {len(steals)}")
for s in steals[:6]:
    print(f"   pts={s[0]:.1f} locked T{s[1]} '{s[2]}' died; unlocked T{s[3]} took the spot")

# --- 5. track longevity + churn ---
lifespans = [(tid, seq[0][0], seq[-1][0]) for tid, seq in by_tid.items()]
total_ids = len(lifespans)
window = events[-1]["pts"] - events[0]["pts"]
print(f"\n5. CHURN: {total_ids} distinct track ids in {window:.0f}s "
      f"({total_ids/ (window/60):.1f} ids/min)")

# dump pathology moments for frame rendering (reel positions)
out = {
    "teleports": [(tp[0], tp[1], tp[2], tp[3]) for tp in teleports],
    "dups": dup_moments[:20],
    "steals": steals[:10],
}
json.dump(out, open("/private/tmp/claude-501/-Users-vives/413cfab1-f36f-4871-b004-67a6ced0e875/scratchpad/pathology_moments.json", "w"), indent=1)
print("\nmoments -> pathology_moments.json")
