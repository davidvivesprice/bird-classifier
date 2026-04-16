#!/usr/bin/env python3
"""bird_pipeline_v3 — Frigate-inspired live detection orchestrator.

See docs/superpowers/specs/2026-04-11-live-detection-v3-design.md
"""
from __future__ import annotations
import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
HLS_DIR = Path.home() / "bird-snapshots" / "hls"

from pipeline.constants import CAMERA_FEEDER, CAMERA_GROUND
# Use a separate dev DB during testing so production data stays clean.
# Set PIPELINE_DB_PATH to override (e.g. for dev: pipeline_v3_dev.db).
_default_db = Path.home() / "bird-snapshots" / "logs" / "pipeline.db"
PIPELINE_DB = Path(os.environ.get("PIPELINE_DB_PATH", str(_default_db)))
REGIONAL_SPECIES_PATH = MODELS_DIR / "chilmark_feeder_species.txt"

# Detection reads from the camera's NATIVE low-res substream (feeder-sub).
# This is produced by the camera itself (not a go2rtc transcode), so it has
# minimal timing offset from the main stream. Lower CPU than decoding 1080p.
CAMERAS_DETECT = {
    CAMERA_FEEDER: "rtsp://127.0.0.1:8554/feeder-sub",
    # Ground camera disabled — free CPU headroom for feeder quality.
    # Re-enable when ground cam detection is prioritized.
    # CAMERA_GROUND: "rtsp://127.0.0.1:8554/ground-sub",
}
CAMERAS_MAIN = {
    CAMERA_FEEDER: "rtsp://127.0.0.1:8554/feeder-main",
    # CAMERA_GROUND: "rtsp://127.0.0.1:8554/ground-main",
}

YOLO_MODEL = str(MODELS_DIR / "yolov8n_bird.onnx")
YARD_MODEL = str(MODELS_DIR / "yard_model.tflite")
YARD_LABELS = str(MODELS_DIR / "yard_model_labels.txt")
AIY_MODEL = str(MODELS_DIR / "aiy_birds_v1.onnx")
AIY_LABELS = str(MODELS_DIR / "inat_bird_labels.txt")

running = True


def load_regional_species() -> set:
    if not REGIONAL_SPECIES_PATH.exists():
        return set()
    with open(REGIONAL_SPECIES_PATH) as f:
        species = {
            line.strip() for line in f
            if line.strip() and line.strip() != "background"
        }
    return species


def shutdown_handler(signum, frame):
    global running
    logging.info("Shutdown signal received")
    running = False


def prune_loop(event_store, hls_root):
    from pipeline.hls_recorder import HlsRecorder
    while running:
        time.sleep(3600)  # hourly
        try:
            cutoff = int((time.time() - 7 * 86400) * 1000)
            event_store.prune_events(older_than_ms=cutoff)
            event_store.daily_checkpoint()
            HlsRecorder.cleanup_old_chunks(hls_root, retention_days=7)
        except Exception as e:
            logging.warning("Prune loop error: %s", e)


def main():
    global running
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("pipeline")

    from solar_utils import is_nighttime

    # Import pipeline modules
    from pipeline.frame_capture import FrameCapture
    from pipeline.motion_gate import MotionGate
    from pipeline.detector import BirdDetector
    from pipeline.tracker import BirdTracker
    from pipeline.classifier import SmartClassifier
    from pipeline.event_store import EventStore
    from pipeline.hls_recorder import HlsRecorder
    from pipeline.health import HealthState, HealthServer
    from pipeline.process_thread import CameraProcessThread
    from pipeline.sse_events import SSEEventServer

    log.info("Starting bird_pipeline_v3...")

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Port configuration for v3 (dev defaults)
    # health=8102, sse=8104
    HEALTH_PORT = int(os.environ.get("PIPELINE_HEALTH_PORT", "8102"))
    SSE_PORT = int(os.environ.get("PIPELINE_SSE_PORT", "8104"))

    # Shared services
    event_store = EventStore(str(PIPELINE_DB))
    health = HealthState()
    health_server = HealthServer(health, port=HEALTH_PORT)
    health_server.start()
    sse_server = SSEEventServer(port=SSE_PORT)
    sse_server.start()

    regional_species = load_regional_species()

    from pipeline.camera_config import CameraClassifierConfig

    camera_configs = {
        CAMERA_FEEDER: CameraClassifierConfig(use_yard=True),
        CAMERA_GROUND: CameraClassifierConfig(use_yard=False),
    }

    # Retry classifier loading with backoff — Coral USB is single-session and
    # may be held by another process (classify.py --watch) after a machine
    # restart. Wait for it to become available instead of crash-looping.
    classifier = None
    for attempt in range(1, 13):  # up to ~2 minutes of retries
        try:
            classifier = SmartClassifier(
                yard_model_path=YARD_MODEL,
                yard_labels_path=YARD_LABELS,
                aiy_model_path=AIY_MODEL,
                aiy_labels_path=AIY_LABELS,
                regional_species=regional_species,
                camera_configs=camera_configs,
            )
            break
        except Exception as e:
            if attempt < 12:
                wait = min(10, attempt * 2)  # 2, 4, 6, 8, 10, 10, 10, ...
                log.warning("Classifier load attempt %d failed: %s — retrying in %ds", attempt, e, wait)
                time.sleep(wait)
            else:
                log.error("Failed to load classifiers after %d attempts: %s — pipeline will not start", attempt, e)
                return 1

    # Per-camera stack
    camera_stacks = []
    for name, detect_url in CAMERAS_DETECT.items():
        main_url = CAMERAS_MAIN[name]
        try:
            frame_q = queue.Queue(maxsize=2)
            # Reads from feeder-sub (camera's native 640x360 substream, see
            # CAMERAS_DETECT above). The HLS recorder below reads feeder-main
            # at full resolution; they're independent RTSP consumers via go2rtc.
            capture = FrameCapture(name, detect_url, out_queue=frame_q,
                                   width=640, height=360, fps=5)
            motion_gate = MotionGate()
            tracker = BirdTracker()
            detector = BirdDetector(
                yolo_model_path=YOLO_MODEL,
                stationary_track_regions_fn=tracker.stationary_regions,
                confidence=0.3,
            )
            process = CameraProcessThread(
                name=name,
                frame_queue=frame_q,
                motion_gate=motion_gate,
                detector=detector,
                tracker=tracker,
                classifier=classifier,
                event_store=event_store,
                health=health,
                sse_server=sse_server,
                frame_width=640,
                frame_height=360,
                capture=capture,
            )
            # HLS recorder: -c copy remux with bounded segments for delayed
            # playback overlay. Fixed settings: hls_list_size=15, delete_segments,
            # program_date_time. CPU <1%, minimal disk (30s rolling window).
            recorder = HlsRecorder(name, main_url, str(HLS_DIR / name))

            capture.start()
            process.start()
            recorder.start()
            camera_stacks.append((name, capture, process, recorder))
            log.info("[%s] Stack started", name)
        except Exception as e:
            log.error("[%s] Failed to start: %s", name, e)

    if not camera_stacks:
        log.error("No camera stacks started — exiting")
        return 1

    # Prune loop
    pruner = threading.Thread(
        target=prune_loop, args=(event_store, HLS_DIR), daemon=True
    )
    pruner.start()

    # Main loop: nighttime pause + shutdown wait
    log.info("Pipeline running with %d camera(s)", len(camera_stacks))
    paused_for_night = False
    night_bypass = os.environ.get("PIPELINE_NIGHT_BYPASS", "0") == "1"
    if night_bypass:
        log.info("PIPELINE_NIGHT_BYPASS=1 — nighttime pause disabled")
    while running:
        # Publish SSE server stats to the shared health section so they
        # show up in the /api/pipeline/health endpoint.
        try:
            health.update_shared("sse", dict(sse_server.stats))
        except Exception:
            pass
        time.sleep(10)
        # Daytime-only detection — HLS recording keeps running independently.
        # PIPELINE_NIGHT_BYPASS=1 forces the pipeline to keep processing even at
        # night (used when verifying v3 after-hours against a recorded test loop).
        night = (not night_bypass) and is_nighttime()
        if night and not paused_for_night:
            for name, cap, _proc, _rec in camera_stacks:
                log.info("[%s] Nighttime pause — stopping capture", name)
                cap.stop()
            paused_for_night = True
        elif not night and paused_for_night:
            for name, cap, _proc, _rec in camera_stacks:
                log.info("[%s] Daytime resume — starting capture", name)
                cap.start()
            paused_for_night = False

    log.info("Shutting down...")
    for name, capture, process, recorder in camera_stacks:
        try: capture.stop()
        except Exception: pass
        try: process.stop()
        except Exception: pass
        try: recorder.stop()
        except Exception: pass
    try: sse_server.stop()
    except Exception: pass
    try: health_server.stop()
    except Exception: pass
    try: event_store.shutdown()
    except Exception: pass
    log.info("Bye")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
