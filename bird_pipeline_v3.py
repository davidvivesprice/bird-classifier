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

# Test override: when set, the pipeline reads from this URL instead of go2rtc.
# Used by tools/sync_replay_assert.py to point at mediamtx-on-iMac (per spec §4).
_test_url = os.environ.get("PIPELINE_TEST_RTSP_URL")
if _test_url:
    log_msg = f"[PIPELINE_TEST_RTSP_URL] overriding camera URLs → {_test_url}"
    # mutate both dicts; harness expects feeder-main behaviour from the test stream
    for k in list(CAMERAS_DETECT.keys()):
        CAMERAS_DETECT[k] = _test_url
    for k in list(CAMERAS_MAIN.keys()):
        CAMERAS_MAIN[k] = _test_url
else:
    log_msg = None
# defer logging until main() so we don't double-log on import

YOLO_MODEL = str(MODELS_DIR / "yolov8n_bird.onnx")
YARD_MODEL = str(MODELS_DIR / "yard_model.tflite")
YARD_LABELS = str(MODELS_DIR / "yard_model_labels.txt")
AIY_MODEL = str(MODELS_DIR / "aiy_birds_v1.onnx")
AIY_LABELS = str(MODELS_DIR / "inat_bird_labels.txt")

# ── Per-camera Area-of-Interest polygons (substream pixel coords) ──
# Motion outside the polygon is ignored by MotionGate → no YOLO call for
# out-of-zone leaves/sky/fence/etc. ~5× reduction in YOLO triggers on feeder.
#
# 2026-04-17: David-approved trapezoid for feeder. Narrower at top (just the
# feeder body), wider at bottom (includes hopping birds under the feeder).
# Excludes sky, branches above the feeder roof, fence corners, and grass away
# from the feeder's immediate area.
#
# Long-term: move to per-camera JSON config + a proper polygon editor. See
# forget-me-nots: "Proper polygon-based AOI/zone system (Frigate-style)".
CAMERA_AOI_POLYGONS = {
    # 2026-05-09: Recalibrated for new camera framing. Orange slice feeder
    # is now in the lower-right of frame (y≈275-355). Old trapezoid cut off
    # at y=306 and missed Orioles entirely.
    #
    # New polygon: wide rectangle below the feeder roof line, full height,
    # spanning the seed feeder + orange slice zone. Excludes: feeder roof /
    # sky (top 28px), far-left grass edge, far-right deck corner.
    # Old (2026-04-17): [(96, 306), (128, 198), (512, 198), (544, 306)]
    CAMERA_FEEDER: [(48, 28), (48, 355), (600, 355), (600, 28)],
    # CAMERA_GROUND: None,  # no AOI; ground cam currently disabled anyway
}

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
            # 2026-04-23: log line so the next regression is observable.
            # If this stops appearing hourly in the service log
            # (iMac: ~/Library/Logs/bird-pipeline.log, Pi: journalctl --user
            # -u bird-pipeline), the prune thread has died — that's the signal.
            logging.info("[prune] events pruned + HLS cleanup done (retention 7d)")
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
    from pipeline.snapshot_writer import SnapshotWriter

    log.info("Starting bird_pipeline_v3...")
    if log_msg:
        log.info(log_msg)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Port configuration — defaults match production unit files.
    # iMac: ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
    # Pi:   ~/.config/systemd/user/bird-pipeline.service
    # Both inject the same values; defaults below stay in sync so anyone
    # running outside the launchctl/systemd context still gets the right ports.
    HEALTH_PORT = int(os.environ.get("PIPELINE_HEALTH_PORT", "8100"))
    SSE_PORT = int(os.environ.get("PIPELINE_SSE_PORT", "8105"))

    # Shared services
    event_store = EventStore(str(PIPELINE_DB))
    health = HealthState()
    health_server = HealthServer(health, port=HEALTH_PORT)
    health_server.start()
    sse_server = SSEEventServer(port=SSE_PORT)
    sse_server.start()
    # 2026-05-10: Single-stream architecture. We decode the camera's main
    # 1920×1080 stream once via PyAV; in-process we downscale to 640×360 for
    # YOLO/motion/classifier and keep the full frame for SnapshotWriter.
    # The hi-res ring buffer + separate ffmpeg-on-mainstream are gone — there
    # is no cross-stream sync to debug. Frame.pts is the canonical clock.
    snapshot_writer = SnapshotWriter()
    snapshot_writer.start()
    log.info("SnapshotWriter started — single-stream, frame.bgr_full as authoritative")

    regional_species = load_regional_species()

    from pipeline.camera_config import CameraClassifierConfig

    camera_configs = {
        # 2026-04-17: Briefly flipped feeder to AIY-only hoping for honest
        # uncertainty over confident-wrong. AIY returns "don't know" on ~82%
        # of feeder crops, so tracks almost never lock species → labels don't
        # appear → UX loses its fast-feedback loop. Yard-model wrongness is
        # real (see forget-me-nots: DATA INTEGRITY AUDIT) but at least labels
        # show up quickly and the overlay feels alive. Keep yard until the
        # audit + retrain lands. Ground stays AIY-only as before.
        CAMERA_FEEDER: CameraClassifierConfig(use_yard=True),
        CAMERA_GROUND: CameraClassifierConfig(use_yard=False),
    }

    # 2026-04-24: PI_MODE=1 switches to the Hailo-backed classifier registry.
    # On iMac (PI_MODE unset), this whole block falls back to SmartClassifier
    # with Coral. On Pi, we instantiate PiClassifier wrapping a ModelRegistry
    # of candidate classifiers (AIY ONNX on CPU + Hailo candidates), and
    # make it hot-swappable via /api/models/switch. Saved as
    # snapshot_writer.classifier so authoritative_classify() works the same.
    PI_MODE = os.environ.get("PI_MODE", "0") == "1"

    classifier = None
    if PI_MODE:
        from pipeline.model_registry import build_default_registry
        from pipeline.pi_classifier import PiClassifier
        # Hailo classifiers cohabit with the Hailo detector via the shared
        # HailoEngine VDevice + HailoRT scheduler — see pipeline/hailo_engine.py
        # and playbook §9 Path 1. No exclude_hailo needed.
        # regional_species filters AIY predictions to Chilmark-plausible species.
        # Without this, the model freely outputs Altamira Oriole, Carolina
        # Chickadee, Hooded Oriole, etc. — birds that don't occur at this latitude.
        registry = build_default_registry(str(BASE_DIR / "models"),
                                          regional_species=regional_species)
        classifier = PiClassifier(registry)
        snapshot_writer.classifier = classifier
        log.info("[PI_MODE] PiClassifier ready. Active model: %s", registry.current_name)
        log.info("[PI_MODE] Candidates: %s",
                 ", ".join(c["name"] for c in registry.list()))
    else:
        # iMac path — SmartClassifier with Coral retry (unchanged).
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
                snapshot_writer.classifier = classifier
                break
            except Exception as e:
                if attempt < 12:
                    wait = min(10, attempt * 2)
                    log.warning("Classifier load attempt %d failed: %s — retrying in %ds", attempt, e, wait)
                    time.sleep(wait)
                else:
                    log.error("Failed to load classifiers after %d attempts: %s — pipeline will not start", attempt, e)
                    return 1

    # Per-camera stack
    camera_stacks = []
    camera_trackers = {}  # Store trackers for health endpoint exposure
    for name in CAMERAS_DETECT.keys():
        main_url = CAMERAS_MAIN[name]
        try:
            frame_q = queue.Queue(maxsize=2)
            # Single-stream: decode the 1920×1080 main stream via PyAV.
            # Downscale to 640×360 in-process for motion/YOLO/classifier;
            # keep full frame on Frame.bgr_full for SnapshotWriter. Same
            # buffer = same camera moment = no cross-stream sync.
            capture = FrameCapture(
                name, main_url, out_queue=frame_q,
                capture_width=1920, capture_height=1080,
                detect_width=640, detect_height=360,
            )
            aoi = CAMERA_AOI_POLYGONS.get(name)
            motion_gate = MotionGate(aoi_polygon=aoi, frame_width=640, frame_height=360)
            if aoi:
                log.info("[%s] MotionGate AOI enabled: %d-point polygon", name, len(aoi))
            tracker = BirdTracker(
                distance_threshold=2.5,   # was 2.0; more forgiving for feeder birds that turn/shift
                hit_counter_max=90,       # was 15; ~3s coast @ 30fps — track survives brief gaps
                initialization_delay=2,   # was 1; require 3 hits before classifying (reduces ghost tracks)
            )
            camera_trackers[name] = tracker
            if PI_MODE:
                from pipeline.hailo_detector import HailoDetector
                # Default Hailo HEF for YOLOv8-s. Env override allowed.
                hef_path = os.environ.get(
                    "PI_YOLO_HEF",
                    "/usr/share/hailo-models/yolov8s_h8l.hef",
                )
                detector = HailoDetector(hef_path=hef_path, confidence=0.3)
                log.info("[PI_MODE] HailoDetector: %s", hef_path)
            else:
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
                snapshot_writer=snapshot_writer,
            )
            # HLS recorder drives the browser overlay sync on iMac via the
            # segments.json wall-clock sidecar (see pipeline/hls_recorder.py
            # and dashboard/live.html). Disabled on Pi: the Pi dashboard uses
            # WebRTC (sub-100 ms latency) so there is no HLS consumer.
            # Overlay sync for Pi needs a native WebRTC solution
            # (RTP timestamp → SSE alignment) — TODO, not yet implemented.
            # Do not re-enable here until that design is settled.
            recorder = None if PI_MODE else HlsRecorder(name, main_url, str(HLS_DIR / name))

            # HLS segmenter — single-stream PTS-aware segmenter writing to
            # ~/bird-snapshots/hls/feeder/, served by existing
            # /api/hls-live/{camera}/{path:path} route. Spec:
            # docs/working/specs/2026-05-10-pi-overlay-sync-bedrock-design.md
            from pipeline.hls_segmenter import HlsSegmenter
            seg_dir = HLS_DIR / name
            hls_segmenter = HlsSegmenter(
                camera=name,
                input_url=main_url,
                out_dir=seg_dir,
                window_segments=30,
                retention_s=60.0,
            )
            hls_segmenter.start()
            log.info("[%s] HlsSegmenter started → %s", name, seg_dir)

            capture.start()
            process.start()
            if recorder:
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
        try:
            # Surface snapshot-writer counters (hires_ok/hires_fail/aiy_relabel/
            # aiy_none/dropped_full/errors) so dawn verification can confirm
            # the new high-res + AIY-authority paths are firing end-to-end.
            health.update_shared("snapshot_writer", dict(snapshot_writer.stats))
        except Exception:
            pass
        try:
            # Surface tracker stats (id_switches, active_tracks) for monitoring
            # tracking robustness and ID-switch rate as proxy for threshold fitness.
            tracker_stats = {}
            for cam_name, tracker in camera_trackers.items():
                tracker_stats[cam_name] = {
                    "id_switches": tracker.id_switches,
                    "active_tracks": len(tracker.tracks),
                }
            health.update_shared("tracker", tracker_stats)
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
        if recorder:
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
