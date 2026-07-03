#!/usr/bin/env python3
"""demo_grade.py — run the annotated demo ONCE through the REAL pipeline
components and print a repeatable "is it getting better?" grade.

This is the deterministic replacement for the looping demo service (which
churns the decoder on EOF and is the wrong tool for grading). It builds the
exact detector + tracker + classifier + vote-lock the live pipeline uses,
feeds every demo frame through CameraProcessThread._process_frame, captures the
same SSE track payloads, and scores them against David's annotation log.

It grades TWO things independently:
  * CLASSIFIER quality — per visit: detected? locked? correct species? latency?
  * TRACKER quality    — track-IDs per visit (fragmentation), duplicate-box rate
                         (two boxes on one bird), ID-switch count.
The tracker metrics matter regardless of which classifier runs behind YOLO.

USAGE (run on the Pi, with the live pipeline STOPPED so the Hailo VDevice is
free — the wrapper handles that):
    systemctl --user stop bird-pipeline.service
    cd ~/bird-classifier && venv/bin/python3 tools/demo_grade.py \
        --video /home/vives/demo360.mp4 \
        --annotations /home/vives/may10.annotations.md \
        --events /tmp/demo_events.jsonl
    systemctl --user start bird-pipeline.service

Re-run any time to grade a change. Compare the TRACKER metrics run-over-run.
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

os.environ.setdefault("PI_MODE", "1")
os.environ["PIPELINE_DRY_RUN"] = "1"  # no DB writes, no snapshots

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

import pipeline.av_log_guard  # noqa: F401  install av_log SEGV guard before av.open
import av  # noqa: E402
from pipeline.frame import Frame  # noqa: E402
from pipeline.tracker import BirdTracker  # noqa: E402
from pipeline.motion_gate import MotionGate  # noqa: E402
from pipeline.process_thread import CameraProcessThread  # noqa: E402
from pipeline.hailo_detector import HailoDetector  # noqa: E402
from pipeline.model_registry import build_default_registry  # noqa: E402
from pipeline.pi_classifier import PiClassifier  # noqa: E402
from tools.annotation_parser import load_annotations_file  # noqa: E402


class _FakeSSE:
    """Captures the exact payload ProcessThread._process_frame builds."""
    def __init__(self): self.records = []
    def emit(self, camera, wall_time_ms, pts, tracks):
        self.records.append({"camera": camera, "wall_time_ms": wall_time_ms,
                             "pts": pts, "tracks": tracks})


class _FakeHealth:
    """No-op health sink — _process_frame calls health.update() unconditionally."""
    def update(self, *a, **k): pass


def build_process_thread():
    try:
        from bird_pipeline_v3 import load_regional_species
        regional = load_regional_species()
    except Exception:
        regional = None
    registry = build_default_registry(str(BASE_DIR / "models"), regional_species=regional)
    floor = float(os.environ.get("PIPELINE_CLASSIFIER_FLOOR", "0.16"))
    classifier = PiClassifier(registry, confident_threshold=floor)
    tracker = BirdTracker(
        distance_threshold=float(os.environ.get("PIPELINE_TRACK_DIST", "2.5")),
        hit_counter_max=int(os.environ.get("PIPELINE_TRACK_HIT_MAX", "150")),
        initialization_delay=int(os.environ.get("PIPELINE_TRACK_INIT_DELAY", "2")),
    )
    detector = HailoDetector(
        hef_path=os.environ.get("PI_YOLO_HEF", "/usr/share/hailo-models/yolov8s_h8l.hef"),
        confidence=float(os.environ.get("PIPELINE_DET_CONF", "0.3")),
    )
    import queue
    sse = _FakeSSE()
    pt = CameraProcessThread(
        name="feeder", frame_queue=queue.Queue(), motion_gate=MotionGate(
            aoi_polygon=None, frame_width=640, frame_height=360),
        detector=detector, tracker=tracker, classifier=classifier,
        event_store=None, health=_FakeHealth(), sse_server=sse,
        frame_width=640, frame_height=360, capture=None, snapshot_writer=None,
    )
    return pt, sse, registry


def replay(video, pt, sse):
    c = av.open(video); s = c.streams.video[0]
    n = 0; t0 = time.time()
    for f in c.decode(s):
        bgr = f.to_ndarray(format="bgr24")
        pts = float(f.pts * s.time_base) if f.pts is not None else n / 30.0
        pt._process_frame(Frame(bgr=bgr, wall_time_ms=pts * 1000.0, camera="feeder",
                                width=640, height=360, pts=pts, bgr_full=bgr,
                                full_width=640, full_height=360))
        n += 1
    c.close()
    print(f"  replayed {n} frames in {time.time()-t0:.1f}s", file=sys.stderr)
    return sse.records


def iou(a, b):
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, ix2-ix1) * max(0, iy2-iy1)
    ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
    return inter/ua if ua > 0 else 0.0


def grade(events, visits):
    def norm(x): return (x or "").strip().lower()
    print("\n================ CLASSIFIER SCORECARD (vs annotations) ================")
    print(f"{'#':>2} {'ground truth':20} {'in-frame':>13} {'det':>4} {'LOCKED AS':22} {'ok':>3} {'lat':>8} {'#ids':>4}")
    n_correct = n_lock = n_det = 0
    total_ids_visits = []
    for i, v in enumerate(visits, 1):
        gt = norm(v.species)
        a, b = v.first_in_frame_s, v.last_in_frame_s
        ia = v.first_identifiable_s if v.first_identifiable_s is not None else a
        ib = v.last_identifiable_s if v.last_identifiable_s is not None else b
        win = [e for e in events if a-0.1 <= e["pts"] <= b+0.1]
        det = any(e["tracks"] for e in win)
        n_det += det
        locked = Counter(); first_correct = None; ids = set()
        for e in win:
            for t in e["tracks"]:
                ids.add(t["track_id"])
                if ia-0.1 <= e["pts"] <= ib+0.1 and t.get("is_locked") and t.get("species"):
                    sp = norm(t["species"]); locked[sp] += 1
                    if sp == gt and first_correct is None:
                        first_correct = e["pts"]
        total_ids_visits.append(len(ids))
        lab = locked.most_common(1)[0][0] if locked else "(never locked)"
        ok = lab == gt
        n_lock += bool(locked); n_correct += ok
        lat = f"{(first_correct-ia)*1000:+.0f}ms" if first_correct is not None else ""
        show = ("✓ " if ok else "  ") + lab
        print(f"{i:>2} {v.species[:20]:20} {a:5.1f}-{b:5.1f} {'yes' if det else 'NO':>4} {show[:22]:22} {'Y' if ok else '·':>3} {lat:>8} {len(ids):>4}")
    print(f"   -> detected {n_det}/{len(visits)}  locked {n_lock}/{len(visits)}  CORRECT {n_correct}/{len(visits)}")

    print("\n================ TRACKER QUALITY (model-independent) ================")
    all_ids = set(t["track_id"] for e in events for t in e["tracks"])
    multi = [e for e in events if len(e["tracks"]) >= 2]
    dup = sum(1 for e in multi if any(
        iou(e["tracks"][i]["bbox"], e["tracks"][j]["bbox"]) > 0.4
        for i in range(len(e["tracks"])) for j in range(i+1, len(e["tracks"]))))
    maxsim = max((len(e["tracks"]) for e in events), default=0)
    print(f"  distinct track IDs total : {len(all_ids):>4}   (ideal ~= # real visits = {len(visits)})")
    print(f"  track IDs per visit      : {total_ids_visits}   (ideal all 1)")
    print(f"  fragmentation ratio      : {len(all_ids)/max(len(visits),1):.1f}x   (ideal 1.0x)")
    print(f"  duplicate-box rate       : {dup}/{len(multi)} multi-track frames = {100*dup/max(len(multi),1):.0f}%   (ideal 0%)")
    print(f"  max simultaneous tracks  : {maxsim}   (multi-bird capability)")
    print("=" * 68)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--annotations", required=True)
    ap.add_argument("--events", default="/tmp/demo_events.jsonl")
    args = ap.parse_args()

    pt, sse, registry = build_process_thread()
    print(f"  model: {registry.current_name}", file=sys.stderr)
    records = replay(args.video, pt, sse)
    with open(args.events, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    visits = load_annotations_file(args.annotations)
    grade(records, visits)
    sys.stdout.flush(); sys.stderr.flush()
    os._exit(0)  # skip GC — avoids a Hailo/onnx teardown SEGV at interpreter exit


if __name__ == "__main__":
    main()
