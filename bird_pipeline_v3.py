#!/usr/bin/env python3
"""bird_pipeline_v3 — Frigate-inspired live detection orchestrator.

See docs/superpowers/specs/2026-04-10-live-detection-v2-design.md
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
PIPELINE_DB = Path.home() / "bird-snapshots" / "logs" / "pipeline.db"
REGIONAL_SPECIES_PATH = MODELS_DIR / "chilmark_feeder_species.txt"

CAMERAS = {
    "feeder": "rtsp://127.0.0.1:8554/feeder-main",
    "ground": "rtsp://127.0.0.1:8554/ground-main",
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
    from pipeline.annotator import FrameAnnotator
    from pipeline.debug_stream import DebugStream
    from pipeline.hls_recorder import HlsRecorder
    from pipeline.health import HealthState, HealthServer
    from pipeline.process_thread import CameraProcessThread

    log.info("Starting bird_pipeline_v3...")

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Port configuration for v3 (dev defaults)
    HEALTH_PORT = int(os.environ.get("PIPELINE_HEALTH_PORT", "8102"))
    DEBUG_STREAM_PORT = int(os.environ.get("PIPELINE_DEBUG_PORT", "8103"))

    # Shared services
    event_store = EventStore(str(PIPELINE_DB))
    health = HealthState()
    health_server = HealthServer(health, port=HEALTH_PORT)
    health_server.start()
    debug_stream = DebugStream(port=DEBUG_STREAM_PORT)
    debug_stream.start()

    regional_species = load_regional_species()
    try:
        classifier = SmartClassifier(
            yard_model_path=YARD_MODEL,
            yard_labels_path=YARD_LABELS,
            aiy_model_path=AIY_MODEL,
            aiy_labels_path=AIY_LABELS,
            regional_species=regional_species,
        )
    except Exception as e:
        log.error("Failed to load classifiers: %s — pipeline will not start", e)
        return 1

    # Per-camera stack
    camera_stacks = []
    for name, url in CAMERAS.items():
        try:
            frame_q = queue.Queue(maxsize=2)
            capture = FrameCapture(name, url, out_queue=frame_q,
                                   width=1920, height=1080, fps=5)
            motion_gate = MotionGate()
            tracker = BirdTracker()
            detector = BirdDetector(
                yolo_model_path=YOLO_MODEL,
                stationary_track_regions_fn=tracker.stationary_regions,
                confidence=0.3,
            )
            annotator = FrameAnnotator(name, debug_stream)
            process = CameraProcessThread(
                name=name,
                frame_queue=frame_q,
                motion_gate=motion_gate,
                detector=detector,
                tracker=tracker,
                classifier=classifier,
                event_store=event_store,
                annotator=annotator,
                health=health,
            )
            recorder = HlsRecorder(name, url, str(HLS_DIR / name))

            capture.start()
            annotator.start()
            process.start()
            recorder.start()
            camera_stacks.append((name, capture, annotator, process, recorder))
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
    while running:
        time.sleep(10)
        # Daytime-only detection — HLS recording keeps running independently
        night = is_nighttime()
        if night and not paused_for_night:
            for name, cap, _ann, _proc, _rec in camera_stacks:
                log.info("[%s] Nighttime pause — stopping capture", name)
                cap.stop()
            paused_for_night = True
        elif not night and paused_for_night:
            for name, cap, _ann, _proc, _rec in camera_stacks:
                log.info("[%s] Daytime resume — starting capture", name)
                cap.start()
            paused_for_night = False

    log.info("Shutting down...")
    for name, capture, annotator, process, recorder in camera_stacks:
        try: capture.stop()
        except Exception: pass
        try: annotator.stop()
        except Exception: pass
        try: process.stop()
        except Exception: pass
        try: recorder.stop()
        except Exception: pass
    try: debug_stream.stop()
    except Exception: pass
    try: health_server.stop()
    except Exception: pass
    try: event_store.shutdown()
    except Exception: pass
    log.info("Bye")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
