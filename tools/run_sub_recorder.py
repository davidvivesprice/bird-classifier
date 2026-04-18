#!/usr/bin/env python3
"""Standalone HLS recorder for feeder-sub stream.

Diagnostic companion for /sync-test. Records the 640x360 substream to
~/bird-snapshots/hls/feeder-sub/ with the same sidecar-manifest approach as
the main-stream recorder. Served by the existing /api/hls-live/{camera}
dashboard route.

Usage:
    python -m tools.run_sub_recorder

Run in background:
    nohup python -m tools.run_sub_recorder > /tmp/sub-recorder.log 2>&1 &
"""
import logging
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.hls_recorder import HlsRecorder

CAMERA = "feeder-sub"
RTSP = "rtsp://127.0.0.1:8554/feeder-sub"
OUT = Path.home() / "bird-snapshots" / "hls" / CAMERA


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("sub-recorder")
    log.info("Starting HLS recorder for %s → %s", RTSP, OUT)

    rec = HlsRecorder(CAMERA, RTSP, str(OUT))
    rec.start()

    running = [True]

    def stop(signum, frame):
        log.info("Shutdown signal received")
        running[0] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        while running[0]:
            time.sleep(5)
            stats = rec.stats
            log.info(
                "chunks=%s restarts=%s manifest_updates=%s last_chunk_age=%s",
                stats.get("chunks_written"),
                stats.get("restarts"),
                stats.get("manifest_updates"),
                int((time.time() * 1000) - (stats.get("last_chunk_ms") or 0)) if stats.get("last_chunk_ms") else None,
            )
    finally:
        log.info("Stopping recorder...")
        rec.stop()


if __name__ == "__main__":
    main()
