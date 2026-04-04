#!/usr/bin/env python3
"""bird_pipeline — unified real-time bird detection pipeline.

Decodes RTSP video from go2rtc's local restream via PyAV, runs motion gate +
YOLO + species classification + IoU tracking, and broadcasts detections via SSE.

Architecture:
    go2rtc RTSP restream → PyAV decode → wall-clock 3 FPS → motion gate →
    YOLO detection → species classification (new tracks only) → tracker →
    SSE broadcast (on change) → keeper frame saving (on track expiry)

Usage:
    python bird_pipeline.py                    # Run with defaults
    python bird_pipeline.py --cameras feeder   # Single camera only
    python bird_pipeline.py --port 8100        # Custom SSE port

SSE endpoint: http://localhost:8100/events
Health check: http://localhost:8100/health
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import numpy as np
from PIL import Image

from bird_inference import (
    YOLODetector, SpeciesClassifier, normalize_species,
    crop_bird,
)
from bird_tracker import BirdTracker
from motion_gate import MotionGate
from solar_utils import is_nighttime

# ──────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────

MODEL_DIR = Path("/Users/vives/bird-classifier/models")
YOLO_MODEL = MODEL_DIR / "yolov8n_bird.onnx"
SPECIES_MODEL = MODEL_DIR / "aiy_birds_v1.onnx"
LABELS = MODEL_DIR / "inat_bird_labels.txt"
REGIONAL = MODEL_DIR / "chilmark_feeder_species.txt"
INCOMING_DIR = Path("/Users/vives/bird-snapshots/incoming")

SSE_PORT = int(os.environ.get("BIRD_PIPELINE_PORT", "8100"))

CAMERAS = {
    "feeder": "feeder-main",
    "ground": "ground-main",
}

# Detection thresholds
DETECTION_CONFIDENCE = 0.3
FRAME_INTERVAL = 0.333          # ~3 FPS wall-clock
FORCED_YOLO_INTERVAL = 10.0     # Seconds between forced YOLO runs
WATCHDOG_TIMEOUT = 60.0         # Restart camera if no frame for this long
HEALTH_FILE = Path("/tmp/bird-pipeline-health.json")
HEALTH_INTERVAL = 30.0          # Write health file every 30s
NIGHT_CHECK_INTERVAL = 60.0     # How often to re-check nighttime status

# ──────────────────────────────────────────────────
# Globals
# ──────────────────────────────────────────────────

running = True
sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()
stream_status: dict[str, dict] = {}
_camera_last_frame: dict[str, float] = {}   # camera → monotonic timestamp

# ──────────────────────────────────────────────────
# Signal handling
# ──────────────────────────────────────────────────

def _shutdown_handler(signum, frame):
    global running
    logging.info("Received signal %d, shutting down...", signum)
    running = False

signal.signal(signal.SIGTERM, _shutdown_handler)
signal.signal(signal.SIGINT, _shutdown_handler)

# ──────────────────────────────────────────────────
# Regional species list
# ──────────────────────────────────────────────────

def load_regional_species() -> set[str] | None:
    if REGIONAL.exists():
        with open(REGIONAL) as f:
            species = {line.strip() for line in f if line.strip() and not line.startswith("#")}
        if species:
            return species
    return None

# ──────────────────────────────────────────────────
# SSE Server
# ──────────────────────────────────────────────────

def broadcast_event(event_data: dict):
    """Send SSE event to all connected clients."""
    msg = f"data: {json.dumps(event_data)}\n\n"
    with sse_lock:
        dead = []
        for q in sse_clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            sse_clients.remove(q)


def broadcast_tracks(camera_name: str, tracks: list[dict],
                     frame_width: int, frame_height: int, session_id: str):
    """Broadcast active tracks as one SSE event."""
    # Strip keeper_data from broadcast (it's binary frame data)
    clean_tracks = []
    for t in tracks:
        clean_tracks.append({
            "track_id": t["track_id"],
            "species": t["species"],
            "bbox": t["bbox"],
            "confidence": t["confidence"],
            "is_new": t.get("is_new", False),
            "age_seconds": t.get("age_seconds", 0),
        })

    event = {
        "type": "tracks",
        "camera": camera_name,
        "tracks": clean_tracks,
        "frame_width": frame_width,
        "frame_height": frame_height,
        "session_id": session_id,
        "timestamp": datetime.now().isoformat(),
    }
    broadcast_event(event)


class SSEHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # Silence default access logging

    def do_GET(self):
        if self.path == "/events":
            self._handle_sse()
        elif self.path == "/health":
            self._handle_health()
        elif self.path == "/metrics":
            self._handle_metrics()
        else:
            self.send_error(404)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # Greeting to prime the HTTP stream
        self.wfile.write(b'data: {"type":"connected"}\n\n')
        self.wfile.flush()

        client_queue: queue.Queue = queue.Queue(maxsize=100)
        with sse_lock:
            sse_clients.append(client_queue)

        try:
            while running:
                try:
                    msg = client_queue.get(timeout=15)
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except queue.Empty:
                    # Keepalive
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with sse_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)

    def _handle_health(self):
        health = {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "streams": {k: {kk: vv for kk, vv in v.items() if kk != "thread"}
                        for k, v in stream_status.items()},
            "sse_clients": len(sse_clients),
        }
        body = json.dumps(health).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_metrics(self):
        metrics = {
            "streams": {k: {kk: vv for kk, vv in v.items() if kk != "thread"}
                        for k, v in stream_status.items()},
            "sse_clients": len(sse_clients),
        }
        body = json.dumps(metrics).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ──────────────────────────────────────────────────
# RTSP Video Reader
# ──────────────────────────────────────────────────

class VideoStreamReader:
    """Connect to go2rtc RTSP restream and decode video frames via PyAV.

    Handles reconnection with exponential backoff.
    """

    def __init__(self, stream_name: str, camera_name: str):
        self.stream_name = stream_name
        self.camera_name = camera_name
        self.url = f"rtsp://127.0.0.1:8554/{stream_name}"
        self._container = None
        self._video_stream = None
        self.width = 0
        self.height = 0
        self.codec = ""

    def connect(self) -> bool:
        """Open RTSP connection. Returns True on success."""
        import av
        try:
            self.close()
            self._container = av.open(
                self.url,
                options={
                    "rtsp_transport": "tcp",
                    "stimeout": "5000000",   # 5s connection timeout
                },
            )
            self._video_stream = self._container.streams.video[0]
            self._video_stream.thread_type = "AUTO"
            self.width = self._video_stream.width
            self.height = self._video_stream.height
            self.codec = self._video_stream.codec_context.name or "unknown"
            logging.info("[%s] Connected: %dx%d %s",
                         self.camera_name, self.width, self.height, self.codec)
            return True
        except Exception as e:
            logging.warning("[%s] Connection failed: %s", self.camera_name, e)
            self.close()
            return False

    def frames(self):
        """Yield PIL Images from the RTSP stream. Stops on error."""
        import av
        if self._container is None:
            return
        try:
            for frame in self._container.decode(self._video_stream):
                yield frame.to_image()
        except (av.error.EOFError, av.error.InvalidDataError, av.error.ExitError) as e:
            logging.warning("[%s] Stream error: %s", self.camera_name, e)
        except Exception as e:
            logging.warning("[%s] Unexpected decode error: %s", self.camera_name, e)

    def close(self):
        """Close the RTSP connection."""
        if self._container is not None:
            try:
                self._container.close()
            except Exception:
                pass
            self._container = None
            self._video_stream = None


# ──────────────────────────────────────────────────
# Keeper Frame Saving
# ──────────────────────────────────────────────────

def save_keeper(camera_name: str, track: dict):
    """Save keeper frame for an expired track (atomic write)."""
    if not track.get("keeper_data"):
        return
    try:
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        fname = f"{camera_name}_{ts}_{track['track_id']}.jpg"
        tmp = INCOMING_DIR / (fname + ".tmp")
        tmp.write_bytes(track["keeper_data"])
        tmp.rename(INCOMING_DIR / fname)
        logging.info("[%s] Keeper saved: %s (%s, %.0f%% conf, %.1fs visit)",
                     camera_name, fname, track["species"],
                     track["keeper_confidence"] * 100, track["duration"])
    except Exception as e:
        logging.error("[%s] Keeper save error: %s", camera_name, e)


# ──────────────────────────────────────────────────
# Camera Loop
# ──────────────────────────────────────────────────

def camera_loop(camera_name: str, stream_name: str):
    """Main loop for a single camera: decode → motion → YOLO → track → broadcast."""
    global _camera_last_frame

    # Per-camera model instances (ONNX not thread-safe with CoreML)
    regional = load_regional_species()
    detector = YOLODetector(str(YOLO_MODEL), confidence=DETECTION_CONFIDENCE)
    classifier = SpeciesClassifier(str(SPECIES_MODEL), str(LABELS),
                                   regional_species=regional)
    tracker = BirdTracker()
    gate = MotionGate(threshold_pct=1.5, resize_width=320)

    reader = VideoStreamReader(stream_name, camera_name)
    backoff = 1.0
    last_process = 0.0
    last_forced_yolo = 0.0
    prev_track_state = None

    stream_status[camera_name] = {
        "connected": False,
        "last_frame": None,
        "detections": 0,
        "keepers_saved": 0,
        "frames_processed": 0,
        "motion_skipped": 0,
    }

    logging.info("[%s] Camera loop started (stream=%s)", camera_name, stream_name)

    while running:
        # Nighttime pause — close RTSP, sleep, resume at sunrise
        if is_nighttime():
            if stream_status[camera_name].get("connected"):
                logging.info("[%s] Nighttime — pausing until sunrise", camera_name)
                reader.close()
                stream_status[camera_name]["connected"] = False
            time.sleep(NIGHT_CHECK_INTERVAL)
            continue

        # Connect / reconnect
        if not reader.connect():
            logging.warning("[%s] Reconnecting in %.0fs...", camera_name, backoff)
            time.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        backoff = 1.0
        stream_status[camera_name]["connected"] = True
        _camera_last_frame[camera_name] = time.monotonic()

        # Decode frames
        for pil_image in reader.frames():
            if not running:
                break

            # Nighttime check (mid-stream)
            if is_nighttime():
                break

            # Wall-clock frame rate control
            now = time.monotonic()
            if now - last_process < FRAME_INTERVAL:
                continue
            last_process = now
            _camera_last_frame[camera_name] = now

            stream_status[camera_name]["frames_processed"] = (
                stream_status[camera_name].get("frames_processed", 0) + 1
            )
            stream_status[camera_name]["last_frame"] = datetime.now().isoformat()

            # RGB → BGR for motion gate (OpenCV expects BGR)
            np_rgb = np.array(pil_image)
            np_bgr = np_rgb[:, :, ::-1]

            # Motion gate + forced periodic YOLO
            force_yolo = (now - last_forced_yolo) > FORCED_YOLO_INTERVAL
            has_motion = gate.has_motion(np_bgr, camera=camera_name)

            if not has_motion and not force_yolo:
                stream_status[camera_name]["motion_skipped"] = (
                    stream_status[camera_name].get("motion_skipped", 0) + 1
                )
                # Still check for expired tracks and save keepers
                _handle_expired_tracks(tracker, camera_name)
                # Broadcast if tracks changed (e.g., all expired)
                current_tracks = tracker.get_active_tracks()
                state_key = [(t["track_id"], tuple(t["bbox"])) for t in current_tracks]
                if state_key != prev_track_state:
                    broadcast_tracks(camera_name, current_tracks,
                                     reader.width, reader.height, tracker.session_id)
                    prev_track_state = state_key
                pil_image.close()
                continue

            if force_yolo:
                last_forced_yolo = now

            # YOLO detection
            t_yolo = time.monotonic()
            try:
                detections = detector.detect(pil_image)
            except Exception as e:
                logging.error("[%s] YOLO error: %s", camera_name, e)
                continue
            yolo_ms = (time.monotonic() - t_yolo) * 1000

            if not detections:
                # No birds — still check expired tracks
                _handle_expired_tracks(tracker, camera_name)
                current_tracks = tracker.get_active_tracks()
                state_key = [(t["track_id"], tuple(t["bbox"])) for t in current_tracks]
                if state_key != prev_track_state:
                    broadcast_tracks(camera_name, current_tracks,
                                     reader.width, reader.height, tracker.session_id)
                    prev_track_state = state_key
                pil_image.close()
                continue

            # Classify each detection
            # Only classify NEW tracks — existing tracks reuse their species
            # First, do a preliminary tracker update to see which are new
            # We need species for new ones, so we classify all detections,
            # but only new tracks actually use the classification result.
            species_list = []
            for det in detections:
                crop = crop_bird(pil_image, det["box"])
                if crop.size[0] < 5 or crop.size[1] < 5:
                    species_list.append("unidentified bird")
                    continue

                t_cls = time.monotonic()
                try:
                    filtered, _raw = classifier.classify(crop)
                    top = filtered[0]
                    species_name = top["common_name"]
                    if species_name in ("background", "unidentified bird"):
                        species_list.append("unidentified bird")
                    else:
                        species_list.append(species_name)
                except Exception as e:
                    logging.error("[%s] Classifier error: %s", camera_name, e)
                    species_list.append("unidentified bird")

            cls_ms = (time.monotonic() - t_yolo) * 1000 - yolo_ms

            # Encode keeper frame (JPEG bytes for saving later)
            buf = io.BytesIO()
            pil_image.save(buf, format="JPEG", quality=90)
            frame_data = buf.getvalue()

            # Update tracker
            tracks = tracker.update(detections, species_list, frame_data=frame_data)

            # Log new detections
            for t in tracks:
                if t.get("is_new"):
                    stream_status[camera_name]["detections"] = (
                        stream_status[camera_name].get("detections", 0) + 1
                    )
                    logging.info(
                        "[%s] Track %d: %s (%.0f%% det, %.0fms yolo, %.0fms cls, new)",
                        camera_name, t["track_id"], t["species"],
                        t["confidence"] * 100, yolo_ms, cls_ms,
                    )

            # Broadcast only on track change
            state_key = [(t["track_id"], tuple(t["bbox"])) for t in tracks]
            if state_key != prev_track_state:
                broadcast_tracks(camera_name, tracks,
                                 reader.width, reader.height, tracker.session_id)
                prev_track_state = state_key

            # Handle expired tracks → save keepers
            _handle_expired_tracks(tracker, camera_name)

            # Close PIL image to prevent memory leak in long sessions
            pil_image.close()

        # Stream ended — close and reconnect
        reader.close()
        stream_status[camera_name]["connected"] = False
        if running:
            logging.info("[%s] Stream ended, reconnecting...", camera_name)
            time.sleep(1)

    reader.close()
    logging.info("[%s] Camera loop stopped", camera_name)


def _handle_expired_tracks(tracker: BirdTracker, camera_name: str):
    """Check for expired tracks and save keeper frames."""
    expired = tracker.get_expired_tracks()
    for track in expired:
        save_keeper(camera_name, track)
        stream_status[camera_name]["keepers_saved"] = (
            stream_status[camera_name].get("keepers_saved", 0) + 1
        )



# ──────────────────────────────────────────────────
# Watchdog Thread
# ──────────────────────────────────────────────────

def watchdog_loop(camera_threads: dict[str, threading.Thread]):
    """Monitor camera threads, restart dead ones, write health file."""
    last_health_write = 0.0

    while running:
        now = time.monotonic()

        # Check each camera thread
        for cam_name, thread in list(camera_threads.items()):
            if not thread.is_alive() and running:
                logging.warning("[watchdog] Camera thread %s died, restarting...", cam_name)
                stream_name = CAMERAS[cam_name]
                new_thread = threading.Thread(
                    target=camera_loop,
                    args=(cam_name, stream_name),
                    daemon=True,
                    name=f"cam-{cam_name}",
                )
                new_thread.start()
                camera_threads[cam_name] = new_thread

            # Check for stale frames (no frame for WATCHDOG_TIMEOUT)
            last_frame = _camera_last_frame.get(cam_name, now)
            if (now - last_frame) > WATCHDOG_TIMEOUT and stream_status.get(cam_name, {}).get("connected"):
                logging.warning("[watchdog] %s: no frame for %.0fs, marking disconnected",
                                cam_name, now - last_frame)
                stream_status[cam_name]["connected"] = False

        # Write health file periodically
        if (now - last_health_write) > HEALTH_INTERVAL:
            _write_health_file()
            last_health_write = now

        time.sleep(5)


def _write_health_file():
    """Write health status to /tmp/bird-pipeline-health.json."""
    try:
        health = {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "pid": os.getpid(),
            "streams": {k: {kk: vv for kk, vv in v.items() if kk != "thread"}
                        for k, v in stream_status.items()},
            "sse_clients": len(sse_clients),
        }
        tmp = HEALTH_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(health, indent=2))
        tmp.rename(HEALTH_FILE)
    except Exception as e:
        logging.debug("Health file write error: %s", e)


# ──────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Unified bird detection pipeline")
    parser.add_argument("--port", type=int, default=SSE_PORT, help="SSE server port")
    parser.add_argument("--cameras", type=str, default="all",
                        help="Cameras to process: all, feeder, ground (comma-separated)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(Path("/Users/vives/bird-snapshots/logs/pipeline.log")),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logging.info("bird_pipeline starting: port=%d", args.port)

    # Ensure incoming directory exists
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)

    # Determine which cameras to process
    cameras = dict(CAMERAS)
    if args.cameras != "all":
        requested = set(args.cameras.split(","))
        cameras = {k: v for k, v in cameras.items() if k in requested}

    if not cameras:
        logging.error("No matching cameras. Available: %s", list(CAMERAS.keys()))
        sys.exit(1)

    logging.info("Cameras: %s", list(cameras.keys()))

    # Start SSE server
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("0.0.0.0", args.port), SSEHandler)
    server.daemon_threads = False
    server.block_on_close = True
    server_thread = threading.Thread(target=server.serve_forever, daemon=False, name="sse-server")
    server_thread.start()
    logging.info("SSE server listening on port %d", args.port)

    # Start camera threads
    camera_threads: dict[str, threading.Thread] = {}
    for cam_name, stream_name in cameras.items():
        t = threading.Thread(
            target=camera_loop,
            args=(cam_name, stream_name),
            daemon=True,
            name=f"cam-{cam_name}",
        )
        t.start()
        camera_threads[cam_name] = t
        logging.info("Started camera thread: %s → %s", cam_name, stream_name)

    # Start watchdog
    wd_thread = threading.Thread(target=watchdog_loop, args=(camera_threads,),
                                 daemon=True, name="watchdog")
    wd_thread.start()
    logging.info("Watchdog started")

    # Wait for shutdown
    while running:
        time.sleep(1)

    logging.info("Shutting down...")
    server.shutdown()
    logging.info("SSE server stopped")

    # Wait briefly for camera threads to notice `running = False`
    for t in camera_threads.values():
        t.join(timeout=5)

    _write_health_file()
    logging.info("bird_pipeline stopped")


if __name__ == "__main__":
    main()
