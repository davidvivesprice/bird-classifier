#!/usr/bin/env python3
"""measure_overlay_drift — non-visual overlay sync diagnostic.

Compares the two independent paths that feed the overlay:

  sub_lag  = now - wall_time_ms of newest SSE event
             (pipeline stamp, via feeder-sub → FrameCapture → pipe-read)

  main_lag = now - completed_ms of newest segment in feeder segments.json
             (HLS recorder stamp, via feeder-main → ffmpeg hls remux → mtime)

Both paths see the same camera. Their lag difference equals the overlay drift:

  overlay_drift ≈ sub_lag - main_lag
    positive → event stamps are behind segment stamps → overlay lags video
    negative → event stamps are ahead of segment stamps → overlay leads video

Usage:
    python tools/measure_overlay_drift.py [--duration 30]
"""
from __future__ import annotations
import argparse
import json
import statistics
import sys
import time
import urllib.request


DASHBOARD_URL = "http://localhost:8099"
MAIN_MANIFEST = f"{DASHBOARD_URL}/api/hls-live/feeder/segments.json"
SUB_MANIFEST  = f"{DASHBOARD_URL}/api/hls-live/feeder-sub/segments.json"
SSE_URL       = f"{DASHBOARD_URL}/api/pipeline/events/sse?camera=feeder"


def fetch_newest_segment_ms(url: str) -> float | None:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as r:
            data = json.loads(r.read())
        segs = data.get("segments", [])
        if not segs:
            return None
        return max(s["completed_ms"] for s in segs if "completed_ms" in s)
    except Exception:
        return None


def sse_newest_wall_ms_tracker():
    """Background-ish: opens SSE, tracks newest wall_time_ms seen.

    Uses a simple line-reader loop in a thread so main loop can sample.
    """
    import threading

    state = {"newest": None, "count": 0}

    def reader():
        while True:
            try:
                req = urllib.request.Request(SSE_URL, headers={"Accept": "text/event-stream"})
                with urllib.request.urlopen(req, timeout=5.0) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data: "):
                            continue
                        try:
                            d = json.loads(line[6:])
                        except Exception:
                            continue
                        w = d.get("wall_time_ms")
                        if w is None:
                            continue
                        if state["newest"] is None or w > state["newest"]:
                            state["newest"] = w
                        state["count"] += 1
            except Exception:
                time.sleep(1)

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    return state


def fmt_ms(v):
    if v is None:
        return "   —  "
    return f"{v:+6.0f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=30, help="sample window in seconds")
    ap.add_argument("--interval", type=float, default=0.5, help="sample interval in seconds")
    args = ap.parse_args()

    print(f"[measuring] {args.duration}s @ {args.interval}s interval")
    print(f"[measuring] dashboards: {DASHBOARD_URL}")
    print(f"[measuring] press Ctrl-C to stop early\n")

    sse = sse_newest_wall_ms_tracker()

    samples = []  # list of (sub_lag_ms, main_lag_ms, drift_ms) when all three known

    fmt_h = f"{'t':>4s}  {'sub_lag':>8s}  {'main_lag':>8s}  {'sub-main':>9s}  {'evts':>5s}  {'note':s}"
    print(fmt_h)
    print("-" * len(fmt_h))

    start = time.time()
    try:
        while (time.time() - start) < args.duration:
            now_ms = time.time() * 1000

            newest_event = sse["newest"]
            newest_main = fetch_newest_segment_ms(MAIN_MANIFEST)
            newest_sub  = fetch_newest_segment_ms(SUB_MANIFEST)

            sub_lag  = (now_ms - newest_event) if newest_event is not None else None
            main_lag = (now_ms - newest_main)  if newest_main  is not None else None
            drift    = (sub_lag - main_lag)    if (sub_lag is not None and main_lag is not None) else None

            note = ""
            if newest_event is None:
                note = "waiting for SSE events (bird-gated)"
            elif sub_lag is not None and sub_lag > 30000:
                note = "SSE event is stale (no recent activity)"

            if drift is not None:
                samples.append((sub_lag, main_lag, drift))

            t_rel = time.time() - start
            print(f"{t_rel:4.1f}  {fmt_ms(sub_lag)}  {fmt_ms(main_lag)}  {fmt_ms(drift)}  {sse['count']:>5d}  {note}")
            sys.stdout.flush()

            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[interrupted]")

    print()
    print(f"[samples with full data] n={len(samples)}  SSE events seen={sse['count']}")
    if len(samples) >= 5:
        drifts = [d for _, _, d in samples]
        sub_lags = [s for s, _, _ in samples]
        main_lags = [m for _, m, _ in samples]

        def stats(xs, label):
            xs = sorted(xs)
            med = statistics.median(xs)
            mean = statistics.mean(xs)
            p5 = xs[int(len(xs) * 0.05)]
            p95 = xs[int(len(xs) * 0.95)]
            print(f"  {label:>10s}  median={med:+6.0f}ms  mean={mean:+6.0f}ms  p5={p5:+6.0f}ms  p95={p95:+6.0f}ms")

        stats(sub_lags, "sub_lag")
        stats(main_lags, "main_lag")
        stats(drifts, "drift")
        print()

        # Interpretation
        med_drift = statistics.median(drifts)
        if abs(med_drift) < 100:
            verdict = "overlay is in sync (< 100ms drift)"
        elif med_drift > 0:
            verdict = f"OVERLAY LAGS VIDEO by ~{med_drift:.0f}ms (boxes will appear behind bird)"
        else:
            verdict = f"OVERLAY LEADS VIDEO by ~{-med_drift:.0f}ms (boxes will appear ahead of bird)"
        print(f"[verdict] {verdict}")
    elif sse["count"] == 0:
        print("[verdict] no SSE events seen — pipeline gated by motion, no birds present during window")
    else:
        print("[verdict] too few samples for statistics")


if __name__ == "__main__":
    main()
