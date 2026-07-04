#!/usr/bin/env python3
"""Census v2 — David's bar, honestly scored despite overlapping windows.
Per species: match annotation windows to system lock-visits by time overlap
(greedy best-overlap). Report: windows covered (right species), covered by
ANY lock (right count, wrong name), phantoms (system visits matching nothing).
"""
import json, sys
from collections import defaultdict

EV = sys.argv[1]
GAP_S = 2.0
# annotation windows per species (may10; in-frame windows)
WINDOWS = {
    "house finch": [(0.0, 127.9)],
    "tufted titmouse": [(33.0, 34.2), (132.1, 134.1)],
    "american goldfinch": [(22.0, 25.4), (25.8, 33.0), (25.4, 43.2), (103.5, 125.0), (25.8, 54.5)],
    "blue jay": [(136.1, 149.4)],
}

events = [json.loads(l) for l in open(EV) if l.strip()]
events.sort(key=lambda e: e["pts"])
seg = {}
for e in events:
    for t in e["tracks"]:
        if not t.get("is_locked") or not t.get("species"): continue
        s = seg.setdefault(t["track_id"], [e["pts"], e["pts"], defaultdict(int)])
        s[1] = e["pts"]; s[2][t["species"].lower()] += 1

# system visits: per species, merge sequential (non-concurrent) segments
by_sp = defaultdict(list)
for tid, (a, b, cnt) in seg.items():
    sp = max(cnt, key=cnt.get)
    by_sp[sp].append([a, b])
sysvisits = []
for sp, ivs in by_sp.items():
    ivs.sort()
    merged = []
    for a, b in ivs:
        placed = False
        for m in merged:
            if m[1] - 0.3 <= a <= m[1] + GAP_S:
                m[1] = max(m[1], b); placed = True; break
        if not placed: merged.append([a, b])
    for m in merged: sysvisits.append((sp, m[0], m[1]))

def overlap(a1, b1, a2, b2): return max(0.0, min(b1, b2) - max(a1, a2))

used = set()
matched_correct = {}    # (sp, widx) -> visit
for sp, wins in WINDOWS.items():
    for wi, (wa, wb) in enumerate(wins):
        best, bo = None, 0.0
        for vi, (vsp, va, vb) in enumerate(sysvisits):
            if vi in used or vsp != sp: continue
            o = overlap(wa, wb, va, vb)
            if o > bo: best, bo = vi, o
        if best is not None and bo > 0.3:
            used.add(best); matched_correct[(sp, wi)] = sysvisits[best]

# windows covered by ANY-species lock (count right, name wrong)
matched_any = {}
for sp, wins in WINDOWS.items():
    for wi, (wa, wb) in enumerate(wins):
        if (sp, wi) in matched_correct: continue
        best, bo = None, 0.0
        for vi, (vsp, va, vb) in enumerate(sysvisits):
            if vi in used: continue
            o = overlap(wa, wb, va, vb)
            if o > bo: best, bo = vi, o
        if best is not None and bo > 0.3:
            used.add(best); matched_any[(sp, wi)] = sysvisits[best]

phantoms = [v for i, v in enumerate(sysvisits) if i not in used]
nwin = sum(len(w) for w in WINDOWS.values())
print(f"windows: {nwin} | correct-species covered: {len(matched_correct)} | "
      f"covered-wrong-name: {len(matched_any)} | uncovered: {nwin-len(matched_correct)-len(matched_any)} | phantoms: {len(phantoms)}")
for (sp, wi), v in sorted(matched_correct.items()): print(f"  ✓ {sp} w{wi}  <- {v[1]:.1f}-{v[2]:.1f}")
for (sp, wi), v in sorted(matched_any.items()):     print(f"  ~ {sp} w{wi}  <- {v[0]} {v[1]:.1f}-{v[2]:.1f}")
for v in phantoms:                                   print(f"  ✗ phantom {v[0]} {v[1]:.1f}-{v[2]:.1f}")
