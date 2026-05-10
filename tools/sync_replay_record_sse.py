"""Connect to the Pi's SSE endpoint, write every event as a JSONL line.

Run while the pipeline is processing the replay video; the resulting
file is consumed by sync_replay_assert.py.

Usage:
    python3 sync_replay_record_sse.py \
        --url http://pi5.local:8099/api/pipeline/events/sse?camera=feeder \
        --duration 1800 \
        --out replay_events.jsonl
"""
import argparse
import json
import sys
import time
from urllib.request import urlopen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--duration", type=float, default=1800.0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    t0 = time.time()
    with urlopen(args.url) as resp, open(args.out, "w") as outf:
        for line in resp:
            line = line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            outf.write(payload + "\n")
            outf.flush()
            if time.time() - t0 > args.duration:
                break
    print(f"wrote events to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
