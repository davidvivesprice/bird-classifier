#!/usr/bin/env python3
"""Benchmark detector + classifier co-scheduled on Hailo-8L.

Resolves playbook §12 empirical unknown #1: actual detector FPS when a
classifier is also loaded on the same VDevice via the ROUND_ROBIN scheduler.

Run on the Pi:
    ~/bird-classifier/venv/bin/python3 tools/bench_hailo_multimodel.py

The live bird-pipeline holds a HailoModel for YOLOv8s (and possibly more
once the multi-model refactor is fully deployed). For a clean reading,
stop the pipeline service first; we're a separate process and will hit
HAILO_DEVICE_IN_USE if the live one still owns the chip.

Workflow:
    systemctl --user stop bird-pipeline
    ~/bird-classifier/venv/bin/python3 tools/bench_hailo_multimodel.py
    systemctl --user start bird-pipeline
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import numpy as np


def _bench_one(model, name: str, dummy_input, n: int) -> dict:
    times_ms = []
    for _ in range(n):
        t0 = time.monotonic()
        model.infer({model.input_names[0]: dummy_input})
        times_ms.append((time.monotonic() - t0) * 1000)
    sorted_times = sorted(times_ms)
    return {
        "name": name,
        "n": n,
        "p50_ms": statistics.median(times_ms),
        "p95_ms": sorted_times[max(0, int(0.95 * n) - 1)],
        "p99_ms": sorted_times[max(0, int(0.99 * n) - 1)],
        "mean_ms": statistics.fmean(times_ms),
        "fps": 1000.0 / statistics.fmean(times_ms),
    }


def _bench_interleaved(det_model, cls_model, det_in, cls_in, n: int) -> dict:
    """Drive both models alternating; measure aggregate throughput +
    per-model latency. The scheduler should interleave between the two
    asynchronously, so one full iter (det → cls) ≈ max(det_ms, cls_ms) +
    scheduler overhead, not det_ms + cls_ms."""
    det_times, cls_times = [], []
    t0 = time.monotonic()
    for _ in range(n):
        td0 = time.monotonic()
        det_model.infer({det_model.input_names[0]: det_in})
        td1 = time.monotonic()
        cls_model.infer({cls_model.input_names[0]: cls_in})
        tc1 = time.monotonic()
        det_times.append((td1 - td0) * 1000)
        cls_times.append((tc1 - td1) * 1000)
    elapsed_s = time.monotonic() - t0
    return {
        "iterations": n,
        "elapsed_s": elapsed_s,
        "det_p50_ms": statistics.median(det_times),
        "cls_p50_ms": statistics.median(cls_times),
        "det_mean_ms": statistics.fmean(det_times),
        "cls_mean_ms": statistics.fmean(cls_times),
        "iters_per_s": n / elapsed_s,
        "combined_per_iter_ms": elapsed_s * 1000.0 / n,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--det", default="/usr/share/hailo-models/yolov8s_h8l.hef")
    ap.add_argument("--cls", default="/usr/share/hailo-models/resnet_v1_50_h8l.hef")
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--warmup", type=int, default=20)
    args = ap.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pipeline.hailo_engine import HailoEngine

    eng = HailoEngine.get()
    det = eng.acquire_model(args.det)
    cls = eng.acquire_model(args.cls)

    # Both detector and classifier HEFs from /usr/share/hailo-models bake
    # any required normalization into the graph; pass raw UINT8 0-255 pixels.
    det_in = np.zeros(det.input_shape(), dtype=np.uint8)
    cls_in = np.zeros(cls.input_shape(), dtype=np.uint8)

    print(f"\n=== Single-model isolated (warmup={args.warmup}, n={args.n}) ===")
    for label, m, x in (("DET", det, det_in), ("CLS", cls, cls_in)):
        for _ in range(args.warmup):
            m.infer({m.input_names[0]: x})
        r = _bench_one(m, label, x, args.n)
        print(f"  {label}: p50={r['p50_ms']:.2f} ms  p95={r['p95_ms']:.2f} ms  "
              f"p99={r['p99_ms']:.2f} ms  mean={r['mean_ms']:.2f} ms  → {r['fps']:.1f} FPS")

    print(f"\n=== Interleaved (DET → CLS → … n={args.n}) ===")
    r = _bench_interleaved(det, cls, det_in, cls_in, args.n)
    print(f"  Total: {args.n} iters in {r['elapsed_s']:.3f} s  "
          f"= {r['iters_per_s']:.1f} iters/s  "
          f"({r['combined_per_iter_ms']:.2f} ms/iter)")
    print(f"  per-call latency under co-schedule:")
    print(f"    DET p50={r['det_p50_ms']:.2f} ms  mean={r['det_mean_ms']:.2f} ms")
    print(f"    CLS p50={r['cls_p50_ms']:.2f} ms  mean={r['cls_mean_ms']:.2f} ms")

    eng.shutdown()


if __name__ == "__main__":
    main()
