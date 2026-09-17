#!/usr/bin/env python3
"""tracker_ab.py — A/B two tracker implementations OFFLINE on a recorded
detection stream, scored against David's annotations. No Pi, no Hailo, no
video: the input is a forensic frames JSONL (tools/forensic_replay.py-style:
one line per frame with `pts` and `raw` = [[[x1,y1,x2,y2], conf], ...],
raw detections down to ~0.05 confidence).

Identity persistence is what we score — the question David asked:
"track a bird through the frame even without a label; know when it's NOT a
new object." Per annotated visit (one bird, arrival→departure):
  ids_per_visit   distinct track_ids whose active frames fall in the visit's
                  in-frame window (ideal 1). Sum(ids-1) = HANDOFFS.
  coverage        share of the visit's frames with an active track (ideal 1.0)
  phantoms        track_ids that never overlap any visit window
  dup_box_rate    frames with >=2 active tracks whose boxes overlap IoU>0.3
                  (two ids on one bird)
  id_switches     the tracker's own adjacency heuristic
  trk_ms          mean update() wall time (CPU cost)

Usage:
  PYTHONPATH=. python3 tools/tracker_ab.py frames.jsonl annotations.md [--trackers v3,v4]
Env knobs (PIPELINE_TRACK_*, PIPELINE_DET_CONF) apply to the v4 run.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.detector import Detection  # noqa: E402
from pipeline.tracker_common import _iou  # noqa: E402
from tools.annotation_parser import load_annotations_file  # noqa: E402

V3_FLOOR = 0.30  # production child threshold before 2026-09-17


def make(kind: str):
    if kind == "v3":
        from pipeline.tracker import BirdTracker
        return BirdTracker(distance_threshold=2.5, hit_counter_max=150, initialization_delay=2), V3_FLOOR
    from pipeline.tracker_v4 import BirdTrackerV4
    return BirdTrackerV4(), float(os.environ.get("PIPELINE_DET_CONF", "0.15"))


def video_frames(path: str, width: int = 640, height: int = 360):
    """Yield decoded BGR frames (resized to the detect size) in decode order —
    the forensic recording's frame index `n` is decode order too."""
    import av
    import cv2
    with av.open(path) as c:
        for fr in c.decode(c.streams.video[0]):
            img = fr.to_ndarray(format="bgr24")
            if img.shape[1] != width or img.shape[0] != height:
                img = cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)
            yield img


def replay(kind: str, frames: list, video: str | None = None):
    tracker, floor = make(kind)
    events, trk_ms = [], []
    vid = video_frames(video) if video else None
    for f in frames:
        img = next(vid, None) if vid is not None else None
        dets = [Detection(box=list(b), confidence=float(c)) for b, c in f["raw"] if c >= floor]
        t0 = time.perf_counter()
        out = tracker.update(dets, f["pts"] * 1000.0, frame_bgr=img)
        trk_ms.append((time.perf_counter() - t0) * 1000.0)
        events.append({
            "pts": f["pts"],
            "tracks": [{"track_id": t.track_id, "bbox": list(t.bbox), "coasting": bool(t.coasting)}
                       for t in out.active],
        })
    return events, tracker, sum(trk_ms) / max(len(trk_ms), 1)


def score(events: list, visits: list) -> dict:
    windows = [(v.id, v.species, v.first_in_frame_s, v.last_in_frame_s)
               for v in visits if v.first_in_frame_s is not None and v.last_in_frame_s is not None]
    # track_id -> list of pts where active (non-coasting counts as presence; coasting too)
    presence = defaultdict(list)
    for e in events:
        for t in e["tracks"]:
            presence[t["track_id"]].append(e["pts"])
    per_visit = {}
    claimed = set()
    for vid, sp, a, b in windows:
        ids = {tid for tid, pts in presence.items() if any(a <= p <= b for p in pts)}
        frames_in = [e for e in events if a <= e["pts"] <= b]
        covered = sum(1 for e in frames_in if e["tracks"])
        per_visit[vid] = {"species": sp, "window": (round(a, 1), round(b, 1)),
                          "ids": sorted(ids), "coverage": round(covered / max(len(frames_in), 1), 3)}
        claimed |= ids
    phantoms = sorted(set(presence) - claimed)
    multi = [e for e in events if len(e["tracks"]) >= 2]
    dup = 0
    for e in multi:
        bs = [t["bbox"] for t in e["tracks"]]
        if any(_iou(bs[i], bs[j]) > 0.3 for i in range(len(bs)) for j in range(i + 1, len(bs))):
            dup += 1
    handoffs = sum(max(len(v["ids"]) - 1, 0) for v in per_visit.values())
    return {
        "visits": len(per_visit),
        "unique_ids": len(presence),
        "handoffs": handoffs,
        "fragmentation": round(len(claimed) / max(len(per_visit), 1), 2),
        "mean_coverage": round(sum(v["coverage"] for v in per_visit.values()) / max(len(per_visit), 1), 3),
        "phantoms": len(phantoms),
        "dup_box_rate": round(dup / max(len(multi), 1), 3),
        "multi_frames": len(multi),
        "per_visit": per_visit,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("frames")
    ap.add_argument("annotations")
    ap.add_argument("--trackers", default="v3,v4")
    ap.add_argument("--video", default=None, help="the demo mp4 (same frames as the recording) — enables appearance ReID")
    ap.add_argument("--json", default=None, help="write full results here")
    args = ap.parse_args()
    frames = [json.loads(l) for l in open(args.frames) if l.strip()]
    visits = load_annotations_file(args.annotations)
    results = {}
    for kind in args.trackers.split(","):
        events, tracker, ms = replay(kind, frames, video=args.video)
        s = score(events, visits)
        s["id_switches"] = tracker.id_switches
        s["trk_ms"] = round(ms, 3)
        results[kind] = s
    keys = ["unique_ids", "handoffs", "fragmentation", "mean_coverage", "phantoms",
            "dup_box_rate", "id_switches", "trk_ms"]
    kinds = list(results)
    print(f"{'metric':<16}" + "".join(f"{k:>12}" for k in kinds))
    for k in keys:
        print(f"{k:<16}" + "".join(f"{results[kd][k]:>12}" for kd in kinds))
    print("\nper-visit ids (ideal: one id per visit):")
    for vid in results[kinds[0]]["per_visit"]:
        row = f"  {vid:<4}{results[kinds[0]]['per_visit'][vid]['species']:<20}"
        for kd in kinds:
            pv = results[kd]["per_visit"][vid]
            row += f"  {kd}: {len(pv['ids'])} ids cov={pv['coverage']:.2f}"
        print(row)
    if args.json:
        Path(args.json).write_text(json.dumps(results, indent=1))


if __name__ == "__main__":
    main()
