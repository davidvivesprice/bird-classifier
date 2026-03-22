#!/usr/bin/env python3
"""
Real-time bird detection overlay via go2rtc frame snapshots.

Polls go2rtc HTTP frame API for JPEG snapshots, runs YOLOv8n detection
+ AIY Birds V1 species classification, and pushes detection events via SSE.

The dashboard overlays bounding boxes + labels on the live video feed.

Uses go2rtc's /api/frame.jpeg endpoint (proxied through nginx on the NAS)
instead of direct RTSP, avoiding FFMPEG dependency issues on macOS.

Usage:
    python live_detector.py                 # Run with default config
    python live_detector.py --fps 5         # Custom FPS target
    python live_detector.py --cameras feeder  # Single camera only

SSE endpoint: http://localhost:8097/events
Health check: http://localhost:8097/health
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import queue
import signal
import ssl
import sys
import threading
import time
import urllib.request
from collections import Counter

from metrics import MetricsRegistry

_metrics = MetricsRegistry()
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image

from bird_inference import SPECIES_ALIASES, normalize_species, parse_label, crop_bird, get_providers

# Coral Edge TPU — disabled by default to avoid stealing TPU from classify.py
# Enable with LIVE_DETECT_CORAL=1 if running without the batch classifier
_CORAL_OK = False
if os.environ.get("LIVE_DETECT_CORAL", "0") == "1":
    try:
        from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
        from pycoral.adapters import common as _coral_common
        _CORAL_OK = bool(list_edge_tpus())
    except ImportError:
        pass

# --- Configuration ---
MODEL_DIR = Path("/Users/vives/bird-classifier/models")
YOLO_MODEL_PATH = MODEL_DIR / "yolov8n_bird.onnx"
SPECIES_MODEL_PATH = MODEL_DIR / "aiy_birds_v1.onnx"
SPECIES_TPU_PATH = MODEL_DIR / "aiy_birds_v1_edgetpu.tflite"
LABELS_PATH = MODEL_DIR / "inat_bird_labels.txt"
REGIONAL_SPECIES_PATH = MODEL_DIR / "chilmark_feeder_species.txt"

# go2rtc frame API (proxied via NAS nginx + Traefik)
# Must use the correct hostname for Traefik's Host-based routing
GO2RTC_HOST = os.environ.get("GO2RTC_HOST", "192.168.5.92")
GO2RTC_PORT = os.environ.get("GO2RTC_PORT", "9444")
GO2RTC_HOSTNAME = os.environ.get("GO2RTC_HOSTNAME", "birds.vivessyn.duckdns.org")
GO2RTC_BASE = f"https://{GO2RTC_HOST}:{GO2RTC_PORT}"

# Camera stream names in go2rtc
CAMERA_STREAMS = {
    "feeder": "feeder-main",
    "ground": "ground-main",
}

# Detection thresholds
BIRD_CLASS_ID = 0
DETECTION_CONFIDENCE = 0.35
NMS_IOU_THRESHOLD = 0.45
CROP_PAD_RATIO = 0.15

# Temporal voting: require consistent species ID before reporting
# A detection at a given position must get the same species N times
# out of the last M frames before we broadcast it.
VOTE_MIN_HITS = 2           # need at least 2 agreeing frames
VOTE_WINDOW = 5             # out of the last 5 classifications at that position
VOTE_IOU_MATCH = 0.3        # IoU threshold to consider same bird across frames
VOTE_COOLDOWN_SEC = 5.0     # don't re-report same species at same position within 5s

# YOLO input
YOLO_INPUT_SIZE = 640
SPECIES_INPUT_SIZE = (224, 224)

# SSE server
SSE_PORT = int(os.environ.get("LIVE_DETECT_PORT", "8097"))
TARGET_FPS = float(os.environ.get("LIVE_DETECT_FPS", "3"))

# Nighttime pause — no birds to detect in the dark, save CPU
LATITUDE = 41.35
LONGITUDE = -70.74
NIGHT_OFFSET_MINUTES = 30  # keep running after sunset

from solar_utils import solar_times, is_nighttime

# Auth cookie for NAS proxy
AUTH_COOKIE = os.environ.get("BIRDS_AUTH_COOKIE", "pW3nRj5vKz")


# ──────────────────────────────────────────────────
# Temporal voting tracker
# ──────────────────────────────────────────────────

def _iou(box_a, box_b):
    """Compute intersection-over-union between two [x1,y1,x2,y2] boxes."""
    xa = max(box_a[0], box_b[0])
    ya = max(box_a[1], box_b[1])
    xb = min(box_a[2], box_b[2])
    yb = min(box_a[3], box_b[3])
    inter = max(0, xb - xa) * max(0, yb - ya)
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0


class SpeciesVoter:
    """Track bird positions across frames and require consistent species ID.

    For each camera, maintains a list of "slots" — tracked bird positions.
    Each slot accumulates a sliding window of species votes from the classifier.
    A detection is only broadcast when the top species has >= VOTE_MIN_HITS
    in the last VOTE_WINDOW classifications, preventing the "flickering species"
    problem where the same bird gets classified as 6 different species.
    """

    def __init__(self):
        # Per-camera slots: { camera: [slot, ...] }
        # Each slot: { box, votes: [species, ...], last_reported: {species: time}, last_seen: time }
        self._slots = {}

    def process(self, camera, detections_with_preds):
        """Process a frame's detections and return only those that pass voting.

        Args:
            camera: camera name (e.g. "feeder")
            detections_with_preds: list of (det_dict, pred_dict) tuples

        Returns:
            list of (det_dict, pred_dict) tuples that should be broadcast
        """
        now = time.monotonic()
        if camera not in self._slots:
            self._slots[camera] = []
        slots = self._slots[camera]

        # Expire stale slots (not seen in 3 seconds)
        slots[:] = [s for s in slots if now - s["last_seen"] < 3.0]

        approved = []

        for det, pred in detections_with_preds:
            box = det["box"]
            species = pred["common_name"]

            # Find matching slot by IoU
            best_slot = None
            best_iou = 0
            for s in slots:
                iou = _iou(box, s["box"])
                if iou > best_iou:
                    best_iou = iou
                    best_slot = s

            if best_slot and best_iou >= VOTE_IOU_MATCH:
                # Update existing slot
                best_slot["box"] = box  # update position
                best_slot["votes"].append(species)
                if len(best_slot["votes"]) > VOTE_WINDOW:
                    best_slot["votes"] = best_slot["votes"][-VOTE_WINDOW:]
                best_slot["last_seen"] = now
                slot = best_slot
            else:
                # New slot
                slot = {
                    "box": box,
                    "votes": [species],
                    "last_reported": {},
                    "last_seen": now,
                }
                slots.append(slot)

            # Check if top species has enough votes
            vote_counts = Counter(slot["votes"])
            top_species, top_count = vote_counts.most_common(1)[0]

            if top_count >= VOTE_MIN_HITS and top_species == species:
                # Check cooldown: don't re-report same species at same position too fast
                last_time = slot["last_reported"].get(top_species, 0)
                if now - last_time >= VOTE_COOLDOWN_SEC:
                    slot["last_reported"][top_species] = now
                    # Use the voted species (might differ from this frame's pred)
                    approved.append((det, pred))

        # Cap slots per camera
        if len(slots) > 20:
            slots[:] = sorted(slots, key=lambda s: s["last_seen"], reverse=True)[:20]

        return approved

running = True


def handle_signal(signum, frame):
    global running
    running = False


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


# ──────────────────────────────────────────────────
# Model loading + inference
# ──────────────────────────────────────────────────

def _get_providers():
    available = ort.get_available_providers()
    providers = []
    if "CoreMLExecutionProvider" in available:
        providers.append("CoreMLExecutionProvider")
    providers.append("CPUExecutionProvider")
    return providers


_species_backend = "onnx"  # set by load_models()


def load_models():
    """Load YOLO + species models."""
    global _species_backend
    providers = _get_providers()
    logging.info("Loading YOLO: %s (providers=%s)", YOLO_MODEL_PATH, providers)
    yolo = ort.InferenceSession(str(YOLO_MODEL_PATH), providers=providers)
    yolo_input = yolo.get_inputs()[0].name
    logging.info("YOLO loaded: providers=%s", yolo.get_providers())

    with open(LABELS_PATH) as f:
        labels = [line.strip() for line in f]

    # Try Coral TPU for species classification
    if _CORAL_OK and SPECIES_TPU_PATH.exists():
        try:
            species = make_interpreter(str(SPECIES_TPU_PATH))
            species.allocate_tensors()
            species_input = None
            _species_backend = "coral"
            logging.info("Species model loaded on CORAL TPU: %s (%d labels)",
                         SPECIES_TPU_PATH.name, len(labels))
        except Exception as e:
            logging.warning("Coral TPU failed (%s), falling back to ONNX", e)
            _species_backend = "onnx"
            species = ort.InferenceSession(str(SPECIES_MODEL_PATH), providers=providers)
            species_input = species.get_inputs()[0].name
            logging.info("Species model loaded: %d labels, providers=%s", len(labels), species.get_providers())
    else:
        _species_backend = "onnx"
        logging.info("Loading species classifier: %s", SPECIES_MODEL_PATH)
        species = ort.InferenceSession(str(SPECIES_MODEL_PATH), providers=providers)
        species_input = species.get_inputs()[0].name
        logging.info("Species model loaded: %d labels, providers=%s", len(labels), species.get_providers())

    regional = None
    if REGIONAL_SPECIES_PATH.exists():
        with open(REGIONAL_SPECIES_PATH) as f:
            regional = {line.strip() for line in f if line.strip()}
        logging.info("Regional filter: %d species", len(regional))

    return yolo, yolo_input, species, species_input, labels, regional


def preprocess_yolo(pil_image, target_size=YOLO_INPUT_SIZE):
    """Preprocess PIL image for YOLOv8."""
    orig_w, orig_h = pil_image.size
    scale = min(target_size / orig_w, target_size / orig_h)
    new_w, new_h = int(orig_w * scale), int(orig_h * scale)
    resized = pil_image.resize((new_w, new_h), Image.BILINEAR)

    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2
    padded = Image.new("RGB", (target_size, target_size), (114, 114, 114))
    padded.paste(resized, (pad_x, pad_y))

    arr = np.array(padded, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)[np.newaxis]
    return arr, scale, pad_x, pad_y


def nms_numpy(boxes, scores, iou_threshold):
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while len(order) > 0:
        i = order[0]
        keep.append(i)
        if len(order) == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        iou = inter / (areas[i] + areas[order[1:]] - inter)
        remaining = np.where(iou <= iou_threshold)[0]
        order = order[remaining + 1]
    return keep


def detect_birds(yolo, yolo_input, pil_image):
    """Run YOLOv8n on PIL image, return detections."""
    orig_w, orig_h = pil_image.size
    tensor, scale, pad_x, pad_y = preprocess_yolo(pil_image)
    output = yolo.run(None, {yolo_input: tensor})[0]
    predictions = output[0].T

    boxes_cxcywh = predictions[:, :4]
    bird_scores = predictions[:, 4 + BIRD_CLASS_ID]
    mask = bird_scores > DETECTION_CONFIDENCE
    if not mask.any():
        return []

    bird_boxes = boxes_cxcywh[mask]
    bird_conf = bird_scores[mask]

    x1 = bird_boxes[:, 0] - bird_boxes[:, 2] / 2
    y1 = bird_boxes[:, 1] - bird_boxes[:, 3] / 2
    x2 = bird_boxes[:, 0] + bird_boxes[:, 2] / 2
    y2 = bird_boxes[:, 1] + bird_boxes[:, 3] / 2
    boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

    keep = nms_numpy(boxes_xyxy, bird_conf, NMS_IOU_THRESHOLD)
    if not keep:
        return []

    detections = []
    for i in keep:
        bx1, by1, bx2, by2 = boxes_xyxy[i]
        ox1 = max(0, min(orig_w, (bx1 - pad_x) / scale))
        oy1 = max(0, min(orig_h, (by1 - pad_y) / scale))
        ox2 = max(0, min(orig_w, (bx2 - pad_x) / scale))
        oy2 = max(0, min(orig_h, (by2 - pad_y) / scale))
        detections.append({
            "box": [int(ox1), int(oy1), int(ox2), int(oy2)],
            "confidence": round(float(bird_conf[i]), 3),
        })
    return detections


def parse_label(raw_label):
    if "(" in raw_label and raw_label.endswith(")"):
        # Use rindex to split on LAST '(' — handles species like "Hawk (Cooper's)"
        idx = raw_label.rindex("(")
        scientific = raw_label[:idx].strip()
        common = raw_label[idx + 1:-1]
        return scientific, common
    return raw_label, raw_label


def classify_species(species_sess, species_input, labels, bird_crop, regional=None):
    """Classify a bird crop (PIL Image). Returns top prediction dict."""
    resized = bird_crop.resize(SPECIES_INPUT_SIZE)
    arr = np.array(resized, dtype=np.uint8)[np.newaxis]

    if _species_backend == "coral":
        _coral_common.set_input(species_sess, arr[0])
        species_sess.invoke()
        scores = np.array(_coral_common.output_tensor(species_sess, 0), dtype=np.float32)
        if scores.ndim == 2:
            scores = scores[0]
    else:
        scores = species_sess.run(None, {species_input: arr})[0][0]

    if regional:
        all_idx = np.argsort(scores)[::-1]
        for idx in all_idx:
            idx = int(idx)
            _, common = parse_label(labels[idx])
            common = SPECIES_ALIASES.get(common, common)
            if common in regional:
                return {
                    "common_name": common,
                    "scientific_name": parse_label(labels[idx])[0],
                    "raw_score": int(scores[idx]),
                }
        return {"common_name": "unidentified", "scientific_name": "unknown", "raw_score": 0}

    top_idx = int(np.argmax(scores))
    scientific, common = parse_label(labels[top_idx])
    common = SPECIES_ALIASES.get(common, common)
    return {
        "common_name": common,
        "scientific_name": scientific,
        "raw_score": int(scores[top_idx]),
    }


# ──────────────────────────────────────────────────
# Frame fetching from go2rtc
# ──────────────────────────────────────────────────

# Persistent HTTP session — reuses TCP/TLS connection across frames.
# The old approach forked a curl subprocess per frame (6/sec), wasting
# ~15% CPU on process creation + TLS negotiation alone.
_ssl_ctx = ssl.create_default_context()
_ssl_ctx.check_hostname = False
_ssl_ctx.verify_mode = ssl.CERT_NONE

_http_opener = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=_ssl_ctx),
    urllib.request.HTTPCookieProcessor(),
)


def fetch_frame(stream_name: str) -> Image.Image | None:
    """Fetch a JPEG frame from go2rtc via persistent HTTPS connection.

    Uses go2rtc's /api/frame.jpeg endpoint, proxied through nginx + Traefik on the NAS.
    Reuses TCP+TLS connection across calls (no subprocess fork overhead).
    """
    url = f"{GO2RTC_BASE}/api/frame.jpeg?src={stream_name}"
    req = urllib.request.Request(url, headers={
        'Host': GO2RTC_HOSTNAME,
        'Cookie': f'birdauth={AUTH_COOKIE}',
    })
    try:
        resp = _http_opener.open(req, timeout=10)
        data = resp.read()
        if len(data) < 1000:
            return None
        return Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:
        logging.debug("[%s] Frame fetch error: %s", stream_name, e)
        return None


# ──────────────────────────────────────────────────
# SSE Server
# ──────────────────────────────────────────────────

sse_clients: list[queue.Queue] = []
sse_lock = threading.Lock()
stream_status: dict[str, dict] = {}


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


class SSEHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == '/events':
            self._handle_sse()
        elif self.path == '/health':
            self._handle_health()
        elif self.path == '/metrics':
            self._handle_metrics()
        else:
            self.send_error(404)

    def _handle_metrics(self):
        data = json.dumps(_metrics.snapshot()).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle_sse(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Connection', 'keep-alive')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()

        # Send an immediate greeting to prime the HTTP/2 data stream.
        # Without this first data frame, Traefik/nginx may buffer the
        # connection and the browser's EventSource never receives messages.
        self.wfile.write(b'data: {"type":"connected"}\n\n')
        self.wfile.flush()

        client_queue = queue.Queue(maxsize=100)
        with sse_lock:
            sse_clients.append(client_queue)

        try:
            while running:
                try:
                    msg = client_queue.get(timeout=15)
                    self.wfile.write(msg.encode())
                    self.wfile.flush()
                except queue.Empty:
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with sse_lock:
                if client_queue in sse_clients:
                    sse_clients.remove(client_queue)

    def _handle_health(self):
        health = {
            "status": "ok",
            "timestamp": datetime.now().isoformat(),
            "streams": stream_status.copy(),
            "sse_clients": len(sse_clients),
        }
        body = json.dumps(health).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ──────────────────────────────────────────────────
# Camera processing loop
# ──────────────────────────────────────────────────

def camera_loop(camera_name: str, stream_name: str,
                yolo, yolo_input, species_sess, species_input,
                labels, regional, fps: float):
    """Process camera: poll frames, detect birds, classify, push SSE."""
    frame_interval = 1.0 / fps
    consecutive_errors = 0
    voter = SpeciesVoter()

    # Initialize detection funnel for metrics
    _metrics.funnel("detection", [
        "frames", "yolo_hits", "classified", "voter_approved", "broadcast",
    ])

    logging.info("[%s] Starting frame polling (stream=%s, %.1f fps)", camera_name, stream_name, fps)
    stream_status[camera_name] = {"connected": False, "last_frame": None, "detections": 0}

    while running:
        # Sleep during nighttime — no birds to detect, saves ~77% CPU
        if is_nighttime():
            if stream_status[camera_name].get("connected"):
                logging.info("[%s] Nighttime — pausing detection until sunrise", camera_name)
                stream_status[camera_name]["connected"] = False
            _metrics.counter("frames_skipped_night").inc()
            time.sleep(60)
            continue

        t_start = time.monotonic()

        frame = fetch_frame(stream_name)
        t_fetch = time.monotonic()
        _metrics.histogram("fetch_ms").record((t_fetch - t_start) * 1000)

        if frame is None:
            _metrics.counter("fetch_errors").inc()
            consecutive_errors += 1
            if consecutive_errors == 10:
                logging.warning("[%s] 10 consecutive frame errors", camera_name)
                stream_status[camera_name]["connected"] = False
            if consecutive_errors > 30:
                time.sleep(5)
            else:
                time.sleep(frame_interval)
            continue

        if consecutive_errors >= 10:
            logging.info("[%s] Reconnected after %d errors", camera_name, consecutive_errors)
        consecutive_errors = 0
        stream_status[camera_name]["connected"] = True
        stream_status[camera_name]["last_frame"] = datetime.now().isoformat()

        _metrics.funnel("detection").inc("frames")
        _metrics.counter("frames_processed").inc()

        # Measure frame brightness (enables adaptive dark-frame skip, ~2ms overhead)
        brightness = float(np.mean(np.array(frame.convert('L'))))
        _metrics.gauge("frame_brightness").set(brightness)

        w, h = frame.size
        t_yolo_start = time.monotonic()

        # Detect birds (wrapped to prevent camera thread death on inference crash)
        try:
            detections = detect_birds(yolo, yolo_input, frame)
        except Exception as e:
            logging.error("[%s] YOLO detection error: %s", camera_name, e)
            _metrics.counter("yolo_errors").inc()
            consecutive_errors += 1
            elapsed = time.monotonic() - t_start
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
            continue

        t_yolo_end = time.monotonic()
        _metrics.histogram("yolo_ms").record((t_yolo_end - t_yolo_start) * 1000)

        if not detections:
            _metrics.counter("frames_no_birds").inc()
            # Sleep remainder of frame interval
            elapsed = time.monotonic() - t_start
            if elapsed < frame_interval:
                time.sleep(frame_interval - elapsed)
            continue

        _metrics.funnel("detection").inc("yolo_hits", len(detections))

        # Classify each detection and collect candidates for voting
        candidates = []  # list of (det_dict, pred_dict)
        for det in detections:
            x1, y1, x2, y2 = det["box"]
            bw, bh = x2 - x1, y2 - y1
            pad_x = int(bw * CROP_PAD_RATIO)
            pad_y = int(bh * CROP_PAD_RATIO)
            cx1 = max(0, x1 - pad_x)
            cy1 = max(0, y1 - pad_y)
            cx2 = min(w, x2 + pad_x)
            cy2 = min(h, y2 + pad_y)

            crop = frame.crop((cx1, cy1, cx2, cy2))
            if crop.size[0] == 0 or crop.size[1] == 0:
                continue

            t_cls_start = time.monotonic()
            try:
                pred = classify_species(species_sess, species_input, labels, crop, regional)
            except Exception as e:
                logging.error("[%s] Classifier error: %s", camera_name, e)
                _metrics.counter("classify_errors").inc()
                continue
            _metrics.histogram("classify_ms").record((time.monotonic() - t_cls_start) * 1000)

            if pred["common_name"] in ("background", "unidentified bird", "unidentified"):
                _metrics.counter("rejected_background").inc()
                continue
            # Skip low-confidence classifier results (likely wrong species ID)
            if pred["raw_score"] < 5:
                _metrics.counter("rejected_low_score").inc()
                continue

            _metrics.funnel("detection").inc("classified")
            _metrics.histogram("raw_score").record(pred["raw_score"])
            candidates.append((det, pred))

        # Temporal voting: only broadcast detections with consistent species ID
        approved = voter.process(camera_name, candidates)
        _metrics.funnel("detection").inc("voter_approved", len(approved))
        rejected_by_voter = len(candidates) - len(approved)
        if rejected_by_voter > 0:
            _metrics.counter("rejected_voter").inc(rejected_by_voter)

        elapsed_ms = (time.monotonic() - t_yolo_start) * 1000
        _metrics.histogram("total_pipeline_ms").record(elapsed_ms)

        for det, pred in approved:
            event = {
                "camera": camera_name,
                "species": pred["common_name"],
                "scientific_name": pred["scientific_name"],
                "confidence": det["confidence"],
                "raw_score": pred["raw_score"],
                "bbox": det["box"],
                "frame_width": w,
                "frame_height": h,
                "timestamp": datetime.now().isoformat(),
                "inference_ms": round(elapsed_ms, 1),
            }
            broadcast_event(event)
            _metrics.funnel("detection").inc("broadcast")
            _metrics.counter("broadcasts").inc()
            stream_status[camera_name]["detections"] = stream_status[camera_name].get("detections", 0) + 1
            logging.info(
                "[%s] %s (%.0f%% det, score=%d, %.0fms, voted)",
                camera_name, event["species"],
                event["confidence"] * 100, event["raw_score"], elapsed_ms,
            )

        # Sleep remainder of frame interval
        elapsed = time.monotonic() - t_start
        if elapsed < frame_interval:
            time.sleep(frame_interval - elapsed)

    logging.info("[%s] Stopped", camera_name)


def main():
    parser = argparse.ArgumentParser(description="Real-time bird detection SSE server")
    parser.add_argument("--fps", type=float, default=TARGET_FPS, help="Target FPS per camera")
    parser.add_argument("--port", type=int, default=SSE_PORT, help="SSE server port")
    parser.add_argument("--cameras", type=str, default="all", help="Cameras to process: all, feeder, ground")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(Path("/Users/vives/bird-snapshots/logs/live_detector.log")),
            logging.StreamHandler(sys.stdout),
        ],
    )

    logging.info("live_detector starting: fps=%.1f, port=%d, go2rtc=%s", args.fps, args.port, GO2RTC_BASE)

    # Load models
    yolo, yolo_input, species, species_input, labels, regional = load_models()

    # Determine which cameras to process
    cameras = dict(CAMERA_STREAMS)
    if args.cameras != "all":
        requested = set(args.cameras.split(","))
        cameras = {k: v for k, v in cameras.items() if k in requested}

    if not cameras:
        logging.error("No matching cameras. Available: %s", list(CAMERA_STREAMS.keys()))
        sys.exit(1)

    # Test connectivity
    for cam_name, stream_name in cameras.items():
        frame = fetch_frame(stream_name)
        if frame:
            logging.info("[%s] Test frame: %dx%d", cam_name, frame.size[0], frame.size[1])
        else:
            logging.warning("[%s] Test frame failed (will retry in loop)", cam_name)

    # Start SSE server (ThreadingHTTPServer allows multiple concurrent SSE clients)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(('0.0.0.0', args.port), SSEHandler)
    server.daemon_threads = False   # allow graceful handler cleanup
    server.block_on_close = True    # join threads on server_close()
    server_thread = threading.Thread(target=server.serve_forever, daemon=False, name='sse-server')
    server_thread.start()
    logging.info("SSE server listening on port %d (threaded)", args.port)

    # Start camera threads
    threads = []
    for cam_name, stream_name in cameras.items():
        t = threading.Thread(
            target=camera_loop,
            args=(cam_name, stream_name, yolo, yolo_input, species, species_input, labels, regional, args.fps),
            daemon=True,
            name=f'cam-{cam_name}',
        )
        t.start()
        threads.append(t)
        logging.info("Started camera thread: %s → %s", cam_name, stream_name)

    # Wait for shutdown
    while running:
        time.sleep(1)

    logging.info("Shutting down...")
    server.shutdown()
    for t in threads:
        t.join(timeout=5)
    logging.info("live_detector stopped")


if __name__ == '__main__':
    main()
