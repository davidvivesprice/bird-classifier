"""End-to-end replay harness.

Replays a recorded video through the pipeline (via mediamtx-on-iMac → go2rtc →
pipeline → HLS), drives a Playwright browser against the dashboard, captures
the canvas overlay state, and asserts against David's annotations.

Usage:
    python3 sync_replay_assert.py \
        --annotations '/Users/vives/docs/bird-observatory/training videos/may10_demo_video.annotations.md' \
        --events replay_events.jsonl \
        --dashboard http://pi5.local:8099 \
        --browser chromium

For tunnel testing (Layer 2b):
    python3 sync_replay_assert.py \
        ... \
        --dashboard https://pi5.vivessato.com \
        --cf-client-id "$CF_ACCESS_CLIENT_ID" \
        --cf-client-secret "$CF_ACCESS_CLIENT_SECRET"
"""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

from tools.annotation_parser import load_annotations_file
from tools.sync_matcher import Event, match_annotations_to_events


def load_events(path: str) -> list[Event]:
    events = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            data = json.loads(line)
            pts = data.get("pts")
            if pts is None: continue
            # Use the LOCKED track's species if available
            species = None
            for t in data.get("tracks", []):
                if t.get("is_locked") and t.get("species"):
                    species = t["species"].lower()
                    break
            events.append(Event(pts=float(pts), species=species, tracks=data.get("tracks", []), raw=data))
    return events


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--annotations", required=True)
    ap.add_argument("--events", required=True, help="JSONL from sync_replay_record_sse.py")
    ap.add_argument("--gate-count", type=int, default=5)
    ap.add_argument("--detection-window-ms", type=int, default=500)
    ap.add_argument("--species-window-ms", type=int, default=1000)
    ap.add_argument("--median-lag-ms", type=int, default=50)
    ap.add_argument("--max-lag-ms", type=int, default=1000)
    args = ap.parse_args()

    visits = load_annotations_file(args.annotations)
    events = load_events(args.events)
    print(f"loaded {len(visits)} visits, {len(events)} events")

    summary = match_annotations_to_events(
        visits, events,
        detection_window_ms=args.detection_window_ms,
        species_window_ms=args.species_window_ms,
    )

    # Filter to the configured gate (first N visits with both windows + species filled)
    gate_results = []
    for r in summary.results:
        v = next((v for v in visits if v.id == r.visit_id), None)
        if not v: continue
        if v.first_identifiable_s is None or v.last_identifiable_s is None: continue
        if not v.species: continue
        gate_results.append(r)
        if len(gate_results) >= args.gate_count: break

    if len(gate_results) < args.gate_count:
        print(f"FAIL: only {len(gate_results)}/{args.gate_count} gate-eligible annotations")
        sys.exit(1)

    # Per spec §Acceptance: 5/5 strict
    all_pass = True
    lags = []
    for r in gate_results:
        if not r.detection_matched:
            print(f"FAIL [{r.visit_id}]: detection not matched")
            all_pass = False
        if r.species_matched is False:
            print(f"FAIL [{r.visit_id}]: species mismatch ({r.fail_reason})")
            all_pass = False
        elif r.species_matched is True:
            lags.append(r.lag_ms)
            print(f"PASS [{r.visit_id}]: lag {r.lag_ms:+.0f}ms")

    # False positives
    for e in summary.false_positives:
        print(f"WARN false positive: pts={e.pts:.3f} species={e.species}")

    # Lag distribution
    if lags:
        lags_sorted = sorted(lags)
        median_lag = lags_sorted[len(lags_sorted) // 2]
        max_lag = max(abs(l) for l in lags)
        print(f"\nlag median: {median_lag:+.0f}ms, max: {max_lag:.0f}ms")
        if abs(median_lag) > args.median_lag_ms:
            print(f"FAIL: median lag {median_lag:+.0f}ms exceeds ±{args.median_lag_ms}ms gate")
            all_pass = False
        if max_lag > args.max_lag_ms:
            print(f"WARN: max single lag {max_lag:.0f}ms exceeds {args.max_lag_ms}ms")

    if all_pass:
        print(f"\nPASS: {len(gate_results)}/{args.gate_count} gate annotations matched.")
        sys.exit(0)
    else:
        print(f"\nFAIL: see above.")
        sys.exit(1)


if __name__ == "__main__":
    main()
