#!/usr/bin/env python3
"""Offline demo replay — the definitive live-ID verification.

The SSE recorder (sync_replay_record_sse.py) hangs on the Pi after ~30s, so it
can never capture a full pass. This harness instead runs the REAL pipeline
components — Hailo detector + Norfair tracker + PiClassifier + calibration + the
actual CameraProcessThread vote-lock — over EVERY frame of a video file, and
captures the exact SSE track payloads _process_frame builds (via a fake
sse_server). No service, no SSE socket, no DB writes, no snapshots. Deterministic,
full-coverage, reproducible.

The output JSONL is byte-compatible with tools.sync_replay_assert, so it scores
directly against David's may10 annotations.

Stop bird-pipeline.service before running (exclusive Hailo VDevice), then restart.

Usage:
    python3 -m tools.offline_replay /home/vives/demo360.mp4 /tmp/offline_events.jsonl
"""
import os
os.environ.setdefault("PI_MODE", "1")
os.environ["PIPELINE_DRY_RUN"] = "1"          # skip DB writes + snapshots
import sys
import json
import time
import queue
from pathlib import Path

import av

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from pipeline.frame import Frame
from pipeline.motion_gate import MotionGate
from pipeline.tracker import BirdTracker
from pipeline.process_thread import CameraProcessThread
from pipeline.hailo_detector import HailoDetector
from pipeline.model_registry import build_default_registry
from pipeline.pi_classifier import PiClassifier
from bird_pipeline_v3 import load_regional_species


class FakeSSE:
    """Captures the exact payload _process_frame builds — no socket."""
    def __init__(self):
        self.events = []

    def emit(self, camera, wall_time_ms, pts, tracks):
        self.events.append({
            "camera": camera,
            "wall_time_ms": wall_time_ms,
            "pts": pts,
            "tracks": tracks,
        })


class FakeHealth:
    def update(self, *a, **k):
        pass


def build_process_thread(sse):
    """Wire the components exactly as bird_pipeline_v3.main() does for PI_MODE."""
    regional = load_regional_species()
    registry = build_default_registry(str(BASE / "models"), regional_species=regional)
    floor = float(os.environ.get("PIPELINE_CLASSIFIER_FLOOR", "0.16"))
    classifier = PiClassifier(registry, confident_threshold=floor)
    tracker = BirdTracker(
        distance_threshold=float(os.environ.get("PIPELINE_TRACK_DIST", "2.5")),
        hit_counter_max=int(os.environ.get("PIPELINE_TRACK_HIT_MAX", "150")),
        initialization_delay=int(os.environ.get("PIPELINE_TRACK_INIT_DELAY", "2")),
    )
    detector = HailoDetector(
        hef_path=os.environ.get("PI_YOLO_HEF", "/usr/share/hailo-models/yolov8s_h8l.hef"),
        confidence=0.3,
    )
    motion_gate = MotionGate(aoi_polygon=None, frame_width=640, frame_height=360)
    print(f"registry current model: {registry.current_name}", file=sys.stderr)
    return CameraProcessThread(
        name="feeder", frame_queue=queue.Queue(), motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=classifier,
        event_store=None, health=FakeHealth(), sse_server=sse,
        frame_width=640, frame_height=360, capture=None, snapshot_writer=None,
    )


def main():
    video, out = sys.argv[1], sys.argv[2]
    sse = FakeSSE()
    pt = build_process_thread(sse)

    container = av.open(video)
    stream = container.streams.video[0]
    tb = stream.time_base
    n = 0
    t0 = time.time()
    for vf in container.decode(stream):
        bgr = vf.to_ndarray(format="bgr24")
        pts = float(vf.pts * tb) if vf.pts is not None else n / 30.0
        frame = Frame(
            bgr=bgr, wall_time_ms=pts * 1000.0, camera="feeder",
            width=bgr.shape[1], height=bgr.shape[0], pts=pts,
            bgr_full=bgr, full_width=bgr.shape[1], full_height=bgr.shape[0],
        )
        try:
            pt._process_frame(frame)
        except Exception as e:
            print(f"frame {n} err: {e}", file=sys.stderr)
        n += 1
        if n % 900 == 0:
            print(f"  {n} frames, pts={pts:.1f}s, {len(sse.events)} events, "
                  f"{n/(time.time()-t0):.1f} fps", file=sys.stderr)

    with open(out, "w") as f:
        for rec in sse.events:
            f.write(json.dumps(rec) + "\n")
    dt = time.time() - t0
    print(f"processed {n} frames in {dt:.1f}s ({n/dt:.1f} fps), "
          f"{len(sse.events)} track-events -> {out}")


if __name__ == "__main__":
    main()
    # Hard-exit to dodge the Hailo GC-teardown SEGV (libhailort crashes during
    # interpreter shutdown AFTER all output is written — harmless but it dumps
    # a ~600MB core every run). Same dodge as tools/demo_grade.py.
    os._exit(0)
