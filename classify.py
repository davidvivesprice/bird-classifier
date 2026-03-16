#!/usr/bin/env python3
"""
Two-stage bird species classifier for feeder camera snapshots.

Stage 1: YOLOv8n detects if a bird is present in the frame (COCO class 14).
Stage 2: AIY Vision Birds V1 classifies the cropped bird region to species.

Images without a detected bird are moved to skipped/ (no false classifications).
Images with a bird are cropped, classified, and organized into classified/{species}/.

Usage:
    python classify.py              # One-shot: classify all pending
    python classify.py --watch      # Watch mode: continuously process new images
    python classify.py --reprocess  # Re-run detection+classification on processed images
    python classify.py --summary    # Print species summary
"""

import argparse
import json
import logging
import math
import os
import shutil
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

import numpy as np
import onnxruntime as ort
from PIL import Image, ImageDraw, ImageFont

# Range filtering for geographic validation
from range_filter import RangeFilter

# Coral Edge TPU — optional, falls back to ONNX+CoreML if unavailable
_CORAL_OK = False
try:
    from pycoral.utils.edgetpu import list_edge_tpus, make_interpreter
    from pycoral.adapters import common as _coral_common
    _CORAL_OK = bool(list_edge_tpus())
except ImportError:
    pass

# --- Configuration ---
BASE_DIR = Path("/Users/vives/bird-snapshots")
INCOMING_DIR = BASE_DIR / "incoming"
CLASSIFIED_DIR = BASE_DIR / "classified"
SKIPPED_DIR = BASE_DIR / "skipped"
FAILED_DIR = BASE_DIR / "failed"
ANNOTATED_DIR = BASE_DIR / "annotated"
LOG_DIR = BASE_DIR / "logs"
MODEL_DIR = Path("/Users/vives/bird-classifier/models")

# Models
YOLO_MODEL_PATH = MODEL_DIR / "yolov8n_bird.onnx"
SPECIES_MODEL_PATH = MODEL_DIR / "aiy_birds_v1.onnx"
SPECIES_TPU_PATH = MODEL_DIR / "aiy_birds_v1_edgetpu.tflite"
LABELS_PATH = MODEL_DIR / "inat_bird_labels.txt"
REGIONAL_SPECIES_PATH = MODEL_DIR / "chilmark_feeder_species.txt"

# Detection thresholds
BIRD_CLASS_ID = 0                 # Custom model: single class "bird"
DETECTION_CONFIDENCE = 0.3        # Min confidence to consider a YOLO detection
NMS_IOU_THRESHOLD = 0.45          # Non-max suppression overlap threshold
CROP_PAD_RATIO = 0.15             # Extra padding around detected bird (15% of box size)

# Watch mode
WATCH_INTERVAL = 10  # seconds
NIGHT_CHECK_INTERVAL = 300  # seconds (5 min) — poll interval when nighttime

# Location: Chilmark, Martha's Vineyard, MA (for sunset/sunrise calculation)
LATITUDE = 41.35
LONGITUDE = -70.74
NIGHT_OFFSET_MINUTES = 30  # keep running this many minutes after sunset

# YOLO input
YOLO_INPUT_SIZE = 640
# Species classifier input
SPECIES_INPUT_SIZE = (224, 224)


def _solar_times(lat, lon, dt=None):
    """Calculate sunrise and sunset hours (UTC) using NOAA simplified algorithm."""
    if dt is None:
        dt = date.today()
    doy = dt.timetuple().tm_yday
    lat_rad = math.radians(lat)
    gamma = 2 * math.pi / 365 * (doy - 1)
    eqtime = 229.18 * (
        0.000075 + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    decl = (
        0.006918 - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    cos_ha = math.cos(math.radians(90.833)) / (
        math.cos(lat_rad) * math.cos(decl)
    ) - math.tan(lat_rad) * math.tan(decl)
    cos_ha = max(-1.0, min(1.0, cos_ha))
    ha = math.degrees(math.acos(cos_ha))
    noon_utc = 720 - 4 * lon - eqtime
    sunrise_utc = (noon_utc - ha * 4) / 60  # hours
    sunset_utc = (noon_utc + ha * 4) / 60   # hours
    return sunrise_utc, sunset_utc


def _utc_offset_for_date(dt):
    """Return UTC offset for US Eastern time (EST=-5, EDT=-4)."""
    year = dt.year
    march1 = date(year, 3, 1)
    nov1 = date(year, 11, 1)
    # DST starts 2nd Sunday of March
    dst_start = march1 + timedelta(days=(6 - march1.weekday()) % 7 + 7)
    # DST ends 1st Sunday of November
    dst_end = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    return -4 if dst_start <= dt <= dst_end else -5


def is_nighttime():
    """Check if it's past sunset+offset or before sunrise for Cape Cod, MA."""
    now = datetime.now()
    today = now.date()
    sunrise_utc, sunset_utc = _solar_times(LATITUDE, LONGITUDE, today)
    offset = _utc_offset_for_date(today)
    sunrise_local = sunrise_utc + offset
    sunset_local = sunset_utc + offset
    current_hours = now.hour + now.minute / 60.0
    sunset_cutoff = sunset_local + NIGHT_OFFSET_MINUTES / 60.0
    return current_hours >= sunset_cutoff or current_hours < sunrise_local


def setup_logging():
    """Configure logging to file and stdout."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "classifier.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


# ──────────────────────────────────────────────────
# Stage 1: YOLOv8n Bird Detection
# ──────────────────────────────────────────────────

def _get_providers():
    """Return ONNX Runtime execution providers, preferring CoreML on macOS."""
    available = ort.get_available_providers()
    providers = []
    if "CoreMLExecutionProvider" in available:
        providers.append("CoreMLExecutionProvider")
    providers.append("CPUExecutionProvider")
    return providers


def load_yolo(path):
    """Load YOLOv8n ONNX model with CoreML acceleration if available."""
    logging.info("Loading YOLO detector: %s", path)
    providers = _get_providers()
    sess = ort.InferenceSession(str(path), providers=providers)
    input_name = sess.get_inputs()[0].name
    active = sess.get_providers()
    logging.info("YOLO loaded: input=%s shape=%s providers=%s", input_name, sess.get_inputs()[0].shape, active)
    return sess, input_name


def preprocess_yolo(image, target_size=YOLO_INPUT_SIZE):
    """
    Preprocess image for YOLOv8: resize, normalize, transpose to NCHW.
    Uses letterbox (pad to square) to preserve aspect ratio.
    Returns (input_tensor, scale_x, scale_y, pad_x, pad_y) for coordinate mapping.
    """
    orig_w, orig_h = image.size

    # Compute scale to fit in target_size while preserving aspect ratio
    scale = min(target_size / orig_w, target_size / orig_h)
    new_w = int(orig_w * scale)
    new_h = int(orig_h * scale)

    # Resize
    resized = image.resize((new_w, new_h), Image.BILINEAR)

    # Pad to target_size x target_size (center)
    pad_x = (target_size - new_w) // 2
    pad_y = (target_size - new_h) // 2
    padded = Image.new("RGB", (target_size, target_size), (114, 114, 114))
    padded.paste(resized, (pad_x, pad_y))

    # To numpy: HWC → CHW, normalize to 0-1, add batch dim
    arr = np.array(padded, dtype=np.float32) / 255.0
    arr = arr.transpose(2, 0, 1)  # CHW
    arr = arr[np.newaxis]  # NCHW

    return arr, scale, pad_x, pad_y


def nms_numpy(boxes, scores, iou_threshold):
    """
    Non-maximum suppression in pure numpy.
    boxes: (N, 4) as x1, y1, x2, y2
    scores: (N,)
    Returns indices to keep.
    """
    if len(boxes) == 0:
        return []

    x1 = boxes[:, 0]
    y1 = boxes[:, 1]
    x2 = boxes[:, 2]
    y2 = boxes[:, 3]
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


def detect_birds(yolo_sess, yolo_input_name, image):
    """
    Run YOLOv8n detection, return list of bird bounding boxes in original image coords.
    Each detection: {"box": (x1, y1, x2, y2), "confidence": float}
    """
    orig_w, orig_h = image.size
    input_tensor, scale, pad_x, pad_y = preprocess_yolo(image)

    # Run inference
    output = yolo_sess.run(None, {yolo_input_name: input_tensor})[0]  # (1, 84, 8400)
    predictions = output[0].T  # (8400, 84)

    # Split: boxes (cx, cy, w, h) and class scores
    boxes_cxcywh = predictions[:, :4]
    class_scores = predictions[:, 4:]  # (8400, 80)

    # Filter for bird class only
    bird_scores = class_scores[:, BIRD_CLASS_ID]
    mask = bird_scores > DETECTION_CONFIDENCE
    if not mask.any():
        return []

    bird_boxes = boxes_cxcywh[mask]
    bird_conf = bird_scores[mask]

    # Convert cx,cy,w,h → x1,y1,x2,y2 (in YOLO 640x640 space)
    x1 = bird_boxes[:, 0] - bird_boxes[:, 2] / 2
    y1 = bird_boxes[:, 1] - bird_boxes[:, 3] / 2
    x2 = bird_boxes[:, 0] + bird_boxes[:, 2] / 2
    y2 = bird_boxes[:, 1] + bird_boxes[:, 3] / 2
    boxes_xyxy = np.stack([x1, y1, x2, y2], axis=1)

    # NMS
    keep = nms_numpy(boxes_xyxy, bird_conf, NMS_IOU_THRESHOLD)
    if not keep:
        return []

    # Map back to original image coordinates
    detections = []
    for i in keep:
        bx1, by1, bx2, by2 = boxes_xyxy[i]
        # Remove padding, undo scale
        ox1 = (bx1 - pad_x) / scale
        oy1 = (by1 - pad_y) / scale
        ox2 = (bx2 - pad_x) / scale
        oy2 = (by2 - pad_y) / scale
        # Clamp to image bounds
        ox1 = max(0, min(orig_w, ox1))
        oy1 = max(0, min(orig_h, oy1))
        ox2 = max(0, min(orig_w, ox2))
        oy2 = max(0, min(orig_h, oy2))

        detections.append({
            "box": (int(ox1), int(oy1), int(ox2), int(oy2)),
            "confidence": round(float(bird_conf[i]), 3),
        })

    return detections


# ──────────────────────────────────────────────────
# Stage 2: Species Classification (AIY Birds V1)
# ──────────────────────────────────────────────────

_species_backend = "onnx"  # set by load_species_model()


def load_species_model(path, labels_path):
    """Load AIY Birds V1 — Coral TPU if available, else ONNX+CoreML."""
    global _species_backend

    with open(labels_path) as f:
        labels = [line.strip() for line in f]

    # Try Coral TPU first
    if _CORAL_OK and SPECIES_TPU_PATH.exists():
        try:
            interp = make_interpreter(str(SPECIES_TPU_PATH))
            interp.allocate_tensors()
            _species_backend = "coral"
            logging.info("Species model loaded on CORAL TPU: %s (%d labels)",
                         SPECIES_TPU_PATH.name, len(labels))
            return interp, None, labels
        except Exception as e:
            logging.warning("Coral TPU failed (%s), falling back to ONNX", e)

    # Fallback: ONNX + CoreML
    logging.info("Loading species classifier (ONNX): %s", path)
    providers = _get_providers()
    sess = ort.InferenceSession(str(path), providers=providers)
    input_name = sess.get_inputs()[0].name
    _species_backend = "onnx"
    logging.info("Species model loaded: %d labels, providers=%s", len(labels), sess.get_providers())
    return sess, input_name, labels


def load_regional_filter(path):
    """Load regional species allowlist. Returns a set of common names, or None if file missing."""
    if not path.exists():
        logging.warning("No regional filter at %s — all species allowed", path)
        return None
    with open(path) as f:
        species = {line.strip() for line in f if line.strip()}
    logging.info("Regional filter loaded: %d species", len(species))
    return species


def parse_label(raw_label):
    """Parse 'Scientific name (Common Name)' into components."""
    if "(" in raw_label and raw_label.endswith(")"):
        scientific = raw_label.split("(")[0].strip()
        common = raw_label.split("(")[1].rstrip(")")
        return scientific, common
    return raw_label, raw_label


def crop_bird(image, box, pad_ratio=CROP_PAD_RATIO):
    """Crop bird region from image with padding."""
    x1, y1, x2, y2 = box
    w = x2 - x1
    h = y2 - y1
    pad_x = int(w * pad_ratio)
    pad_y = int(h * pad_ratio)

    cx1 = max(0, x1 - pad_x)
    cy1 = max(0, y1 - pad_y)
    cx2 = min(image.width, x2 + pad_x)
    cy2 = min(image.height, y2 + pad_y)

    return image.crop((cx1, cy1, cx2, cy2))


def classify_species(species_sess, species_input_name, labels, bird_crop, regional_species=None):
    """
    Classify a cropped bird image.
    Returns (filtered_predictions, raw_predictions).
    If regional_species is set, filtered_predictions only contains regional matches.
    """
    resized = bird_crop.resize(SPECIES_INPUT_SIZE)
    arr = np.array(resized, dtype=np.uint8)[np.newaxis]

    if _species_backend == "coral":
        _coral_common.set_input(species_sess, arr[0])
        species_sess.invoke()
        scores = np.array(_coral_common.output_tensor(species_sess, 0), dtype=np.float32)
        if scores.ndim == 2:
            scores = scores[0]
    else:
        scores = species_sess.run(None, {species_input_name: arr})[0][0]

    # Raw top 3
    top3_idx = np.argsort(scores)[-3:][::-1]
    raw_predictions = []
    for idx in top3_idx:
        idx = int(idx)
        raw_score = int(scores[idx])
        scientific, common = parse_label(labels[idx])
        raw_predictions.append({
            "index": idx,
            "label": labels[idx],
            "scientific_name": scientific,
            "common_name": common,
            "raw_score": raw_score,
        })

    if regional_species is None:
        return raw_predictions, raw_predictions

    # Filter: walk all scores descending, pick top 3 regional matches
    all_idx = np.argsort(scores)[::-1]
    filtered = []
    for idx in all_idx:
        idx = int(idx)
        scientific, common = parse_label(labels[idx])
        if common in regional_species:
            filtered.append({
                "index": idx,
                "label": labels[idx],
                "scientific_name": scientific,
                "common_name": common,
                "raw_score": int(scores[idx]),
            })
            if len(filtered) >= 3:
                break

    if not filtered:
        # No regional match — return as unidentified
        filtered = [{
            "index": -1,
            "label": "unidentified",
            "scientific_name": "unknown",
            "common_name": "unidentified bird",
            "raw_score": 0,
        }]

    return filtered, raw_predictions


# ──────────────────────────────────────────────────
# Annotation: draw bounding boxes + labels on image
# ──────────────────────────────────────────────────

def annotate_image(image, detections, all_predictions, best_idx=0):
    """
    Draw bounding boxes and species labels on a copy of the image.
    all_predictions: list parallel to detections — each element is a list of
                     top predictions for that detection (or None/[] if not classified).
    Returns annotated PIL Image.
    """
    img = image.copy()
    draw = ImageDraw.Draw(img)

    # Try to get a readable font, fall back to default
    font = None
    font_small = None
    for size, small_size in [(28, 18), (24, 16), (20, 14)]:
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
            font_small = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", small_size)
            break
        except (OSError, IOError):
            continue
    if font is None:
        font = ImageFont.load_default()
        font_small = font

    # Color palette for multiple birds — best is green, rest cycle through colors
    OTHER_COLORS = [
        (255, 255, 0),   # yellow
        (0, 200, 255),   # cyan
        (255, 128, 0),   # orange
        (200, 100, 255), # purple
    ]

    for i, det in enumerate(detections):
        x1, y1, x2, y2 = det["box"]
        conf = det["confidence"]
        is_best = (i == best_idx)
        preds = all_predictions[i] if i < len(all_predictions) else []

        # Box color: green for best detection, cycle colors for others
        if is_best:
            color = (0, 255, 0)
            width = 3
        else:
            color = OTHER_COLORS[(i - (1 if i > best_idx else 0)) % len(OTHER_COLORS)]
            width = 2
        draw.rectangle([x1, y1, x2, y2], outline=color, width=width)

        if preds:
            top = preds[0]
            label = f'{top["common_name"]} ({top["raw_score"]})'
            conf_label = f'det: {conf:.0%}'

            # Measure text to position label outside the bounding box
            label_bbox = draw.textbbox((0, 0), label, font=font)
            label_w = label_bbox[2] - label_bbox[0]
            label_h = label_bbox[3] - label_bbox[1]
            conf_bbox = draw.textbbox((0, 0), conf_label, font=font_small)
            conf_h = conf_bbox[3] - conf_bbox[1]
            pad = 6
            gap = 3
            total_h = pad + label_h + gap + conf_h + pad

            # Place label ABOVE the box if room, otherwise BELOW
            # IMPORTANT: Always place OUTSIDE the bounding box to avoid obscuring bird
            if y1 - total_h - 5 >= 0:  # Extra 5px margin above box
                block_top = y1 - total_h - 5
                label_position = "above"
            else:
                block_top = y2 + 5  # Extra 5px margin below box
                label_position = "below"

            species_y = block_top + pad
            conf_y = species_y + label_h + gap

            # Draw dark background behind text (larger to ensure full coverage)
            # Expand left/right to ensure text is fully covered
            bg_left = max(0, x1 - pad)
            bg_right = min(img.width, x1 + label_w + 2 * pad)
            bg_top = max(0, block_top)
            bg_bottom = min(img.height, block_top + total_h)

            # Use semi-transparent black background for contrast
            if img.mode == 'RGBA':
                overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                overlay_draw = ImageDraw.Draw(overlay)
                overlay_draw.rectangle([bg_left, bg_top, bg_right, bg_bottom], fill=(0, 0, 0, 180))
                img.paste(Image.alpha_composite(img.convert('RGBA'), overlay).convert(img.mode))
                draw = ImageDraw.Draw(img)
            else:
                # For RGB images, use solid dark background
                draw.rectangle([bg_left, bg_top, bg_right, bg_bottom], fill=(0, 0, 0))

            # Draw text with contrasting color
            draw.text((x1, species_y), label, fill=color, font=font)
            draw.text((x1, conf_y), conf_label, fill=(200, 200, 200), font=font_small)
        else:
            draw.text((x1, y1 - 20), f'bird {conf:.0%}', fill=color, font=font_small)

    # Top 3 for the BEST detection in bottom-left corner
    best_preds = all_predictions[best_idx] if best_idx < len(all_predictions) else []
    if best_preds:
        y_off = img.height - 80
        for j, p in enumerate(best_preds[:3]):
            line = f'#{j+1} {p["common_name"]} (raw={p["raw_score"]})'
            draw.text((10, y_off), line, fill=(255, 255, 255), font=font_small)
            y_off += 22

    return img


# ──────────────────────────────────────────────────
# Pipeline: Detect → Crop → Classify → Organize
# ──────────────────────────────────────────────────

def extract_camera(filename):
    """Extract camera name from filename prefix.

    'feeder_2026-03-14_16-11-09.jpg' → 'feeder'
    'ground_2026-03-14_16-11-09.jpg' → 'ground'
    '2026-03-14_16-11-09.jpg'        → 'feeder' (default, old format)
    """
    stem = filename.rsplit(".", 1)[0]
    # Check if the first part is a camera name (not a date)
    first = stem.split("_", 1)[0]
    # Dates start with 4 digits (YYYY); camera names don't
    if first and not first[:4].isdigit():
        return first
    return "feeder"


def _strip_camera_prefix(filename):
    """Strip camera prefix from filename, returning just the timestamp portion.

    'feeder_2026-03-14_16-11-09.jpg' → '2026-03-14_16-11-09'
    '2026-03-14_16-11-09.jpg'        → '2026-03-14_16-11-09'
    """
    stem = filename.rsplit(".", 1)[0]
    first = stem.split("_", 1)[0]
    if first and not first[:4].isdigit():
        # Has camera prefix — strip it
        return stem.split("_", 1)[1] if "_" in stem else stem
    return stem


def extract_timestamp(filename):
    """Extract timestamp from filename like 2026-03-02_11-10-42.jpg → '2026-03-02 11:10:42'.

    Also handles camera-prefixed filenames:
    'feeder_2026-03-14_16-11-09.jpg' → '2026-03-14 16:11:09'
    """
    try:
        ts_part = _strip_camera_prefix(filename)
        parts = ts_part.split("_", 1)
        if len(parts) == 2:
            date_part, time_part = parts
            return date_part + " " + time_part.replace("-", ":")
        return ts_part
    except Exception:
        return None


def append_result(result):
    """Append result to JSONL log."""
    log_file = LOG_DIR / "classifications.jsonl"
    with open(log_file, "a") as f:
        f.write(json.dumps(result) + "\n")


def sanitize_dirname(name):
    """Convert a species name to a safe directory name."""
    return name.replace(" ", "_").replace("'", "").replace("/", "-")


def process_file(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, image_path, regional_species=None, range_filter=None):
    """Full pipeline: detect birds → classify species → move file."""
    fname = os.path.basename(image_path)

    try:
        img = Image.open(image_path).convert("RGB")
    except Exception as e:
        logging.error("Failed to open %s: %s", fname, e)
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(image_path), str(FAILED_DIR / fname))
        return None

    t0 = time.monotonic()

    # Stage 1: Bird detection
    detections = detect_birds(yolo_sess, yolo_input_name, img)
    detect_ms = (time.monotonic() - t0) * 1000

    if not detections:
        # No bird found — log it and DELETE the frame (no value for training/review)
        result = {
            "file": fname,
            "timestamp": datetime.now().isoformat(),
            "source_timestamp": extract_timestamp(fname),
            "camera": extract_camera(fname),
            "action": "no_bird",
            "detect_ms": round(detect_ms, 1),
            "detections": 0,
        }
        append_result(result)
        # Delete the empty frame to save storage
        try:
            image_path.unlink()
            logging.info("DELETE %s — no bird detected (%.0fms)", fname, detect_ms)
        except Exception as e:
            logging.warning("Could not delete %s: %s", fname, e)
        return result

    # Stage 2: Classify ALL detected birds (sorted by confidence, best first)
    detections.sort(key=lambda d: d["confidence"], reverse=True)
    best_det = detections[0]
    best_idx = 0

    t1 = time.monotonic()

    # Classify each detection
    all_predictions = []   # list of filtered predictions per detection
    all_raw = []           # list of raw predictions per detection
    for det in detections:
        bird_crop = crop_bird(img, det["box"])
        preds, raw_preds = classify_species(
            species_sess, species_input_name, labels, bird_crop, regional_species
        )
        all_predictions.append(preds)
        all_raw.append(raw_preds)

    classify_ms = (time.monotonic() - t1) * 1000
    total_ms = (time.monotonic() - t0) * 1000

    top = all_predictions[0][0]  # best detection's top prediction

    # Skip if the best bird's species model says "background" or unidentified
    if top["common_name"] in ("background", "unidentified bird"):
        action = "skipped:background" if top["common_name"] == "background" else "skipped:unidentified"
        result = {
            "file": fname,
            "timestamp": datetime.now().isoformat(),
            "source_timestamp": extract_timestamp(fname),
            "camera": extract_camera(fname),
            "action": action,
            "detect_ms": round(detect_ms, 1),
            "classify_ms": round(classify_ms, 1),
            "total_ms": round(total_ms, 1),
            "detections": len(detections),
            "best_detection": best_det,
            "raw_top3": [
                {"common_name": p["common_name"], "scientific_name": p["scientific_name"], "raw_score": p["raw_score"]}
                for p in all_raw[0]
            ],
        }
        append_result(result)
        SKIPPED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(image_path), str(SKIPPED_DIR / fname))
        logging.info("SKIP %s — %s (raw top: %s, %dms)", fname, action, all_raw[0][0]["common_name"], total_ms)
        return result

    # Apply range filter if available
    range_filter_info = {}
    if range_filter:
        src_timestamp = extract_timestamp(fname)
        filter_result = range_filter.filter_detection(
            top["common_name"],
            confidence=best_det["confidence"],
            latitude=LATITUDE,
            longitude=LONGITUDE,
            date=src_timestamp
        )
        if not filter_result["valid"]:
            top_original = top.copy()
            top = {
                "common_name": "unidentified",
                "scientific_name": "unknown",
                "raw_score": 0
            }
            range_filter_info = {
                "range_filter_applied": True,
                "original_species": filter_result["original_species"],
                "filter_reason": filter_result["reason"],
                "filter_flags": filter_result.get("flags", [])
            }
            logging.info("RANGE_FILTER: %s invalid for location — marked as unidentified (%s)",
                        filter_result["original_species"], filter_result["reason"])

    # Success: bird(s) detected and classified
    species_dir = CLASSIFIED_DIR / sanitize_dirname(top["common_name"])
    species_dir.mkdir(parents=True, exist_ok=True)

    # Build per-bird array for multi-bird data
    birds = []
    for idx, (det, preds, raw_preds) in enumerate(zip(detections, all_predictions, all_raw)):
        bird_top = preds[0] if preds else None
        if bird_top and bird_top["common_name"] not in ("background", "unidentified bird"):
            birds.append({
                "detection": det,
                "species": bird_top["common_name"],
                "scientific_name": bird_top["scientific_name"],
                "raw_score": bird_top["raw_score"],
                "top3": [
                    {"common_name": p["common_name"], "scientific_name": p["scientific_name"], "raw_score": p["raw_score"]}
                    for p in preds
                ],
            })

    result = {
        "file": fname,
        "timestamp": datetime.now().isoformat(),
        "source_timestamp": extract_timestamp(fname),
        "camera": extract_camera(fname),
        "action": "classified",
        "detect_ms": round(detect_ms, 1),
        "classify_ms": round(classify_ms, 1),
        "total_ms": round(total_ms, 1),
        "detections": len(detections),
        "best_detection": best_det,
        # Primary bird (backward compatible)
        "top_prediction": {
            "common_name": top["common_name"],
            "scientific_name": top["scientific_name"],
            "raw_score": top["raw_score"],
        },
        "top3": [
            {
                "common_name": p["common_name"],
                "scientific_name": p["scientific_name"],
                "raw_score": p["raw_score"],
            }
            for p in all_predictions[0]
        ],
        "raw_top3": [
            {"common_name": p["common_name"], "scientific_name": p["scientific_name"], "raw_score": p["raw_score"]}
            for p in all_raw[0]
        ],
        # All birds in this frame
        "birds": birds,
    }
    # Add range filter info if present
    if range_filter_info:
        result.update(range_filter_info)
    append_result(result)

    # Save annotated image with bounding box + labels for ALL birds
    annotated = annotate_image(img, detections, all_predictions, best_idx)
    ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)
    annotated.save(str(ANNOTATED_DIR / fname), quality=90)

    shutil.move(str(image_path), str(species_dir / fname))

    raw_note = ""
    if all_raw[0][0]["common_name"] != top["common_name"]:
        raw_note = f" (raw: {all_raw[0][0]['common_name']})"
    bird_count = f" +{len(birds)-1} more" if len(birds) > 1 else ""
    logging.info(
        "BIRD %s → %s%s%s (det=%.0f%%, score=%d, %dms)",
        fname,
        top["common_name"],
        raw_note,
        bird_count,
        best_det["confidence"] * 100,
        top["raw_score"],
        total_ms,
    )
    return result


def get_pending_files():
    """Get JPEG files in incoming/ ready for processing."""
    if not INCOMING_DIR.exists():
        return []
    files = sorted(INCOMING_DIR.glob("*.jpg"))
    return [f for f in files if not f.name.endswith(".tmp")]


def process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species=None, range_filter=None):
    """Process all pending files."""
    files = get_pending_files()
    if not files:
        return 0

    results = []
    for fpath in files:
        r = process_file(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, fpath, regional_species, range_filter)
        if r:
            results.append(r)

    if results:
        classified = [r for r in results if r["action"] == "classified"]
        skipped = [r for r in results if r["action"].startswith("skipped")]

        if classified:
            species_counts = {}
            for r in classified:
                name = r["top_prediction"]["common_name"]
                species_counts[name] = species_counts.get(name, 0) + 1
            logging.info(
                "Batch: %d classified — %s",
                len(classified),
                ", ".join(f"{v}× {k}" for k, v in sorted(species_counts.items())),
            )
        if skipped:
            logging.info("Batch: %d skipped (no bird)", len(skipped))

    return len(results)


def watch_mode(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species=None, range_filter=None):
    """Continuously watch for new files. Pauses during nighttime."""
    logging.info("Watch mode started (polling every %ds)", WATCH_INTERVAL)
    was_night = False
    try:
        while True:
            if is_nighttime():
                if not was_night:
                    logging.info("Nighttime — pausing classification until sunrise")
                    was_night = True
                time.sleep(NIGHT_CHECK_INTERVAL)
                continue
            if was_night:
                logging.info("Daytime resumed — restarting classification")
                was_night = False
            n = process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species, range_filter)
            if n > 0:
                logging.info("Processed %d file(s), waiting for more...", n)
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        logging.info("Watch mode stopped")


def reprocess(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species=None, range_filter=None):
    """Move images from classified/ and skipped/ back to incoming/ and reprocess."""
    count = 0
    for src_dir in [CLASSIFIED_DIR, SKIPPED_DIR]:
        if not src_dir.exists():
            continue
        for fpath in sorted(src_dir.rglob("*.jpg")):
            dest = INCOMING_DIR / fpath.name
            shutil.move(str(fpath), str(dest))
            count += 1

    if count == 0:
        logging.info("No files to reprocess")
        return

    logging.info("Moved %d files back to incoming/, reprocessing...", count)
    process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species, range_filter)


def print_summary():
    """Print summary of all classification results."""
    jsonl_file = LOG_DIR / "classifications.jsonl"
    if not jsonl_file.exists():
        print("No classification results yet.")
        return

    species_counts = {}
    total = 0
    skipped = 0

    with open(jsonl_file) as f:
        for line in f:
            r = json.loads(line)
            total += 1
            if r["action"] == "classified":
                name = r["top_prediction"]["common_name"]
                score = r["top_prediction"]["raw_score"]
                conf = r.get("best_detection", {}).get("confidence", 0)
                species_counts.setdefault(name, []).append((score, conf))
            elif r["action"].startswith("skipped"):
                skipped += 1

    print(f"\n{'='*65}")
    print(f"Bird Classifier Summary (detect → classify)")
    print(f"{'='*65}")
    print(f"Total processed:  {total}")
    print(f"Birds detected:   {total - skipped}")
    print(f"No bird (skipped): {skipped}")
    print()

    if species_counts:
        print(f"{'Species':<30} {'Count':>5}  {'Avg Det%':>8}  {'Avg Score':>9}")
        print(f"{'-'*30} {'-'*5}  {'-'*8}  {'-'*9}")
        for name, data in sorted(species_counts.items(), key=lambda x: -len(x[1])):
            scores = [d[0] for d in data]
            confs = [d[1] for d in data]
            avg_score = sum(scores) / len(scores)
            avg_conf = sum(confs) / len(confs) * 100 if confs else 0
            print(f"{name:<30} {len(data):>5}  {avg_conf:>7.1f}%  {avg_score:>9.1f}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Two-stage bird species classifier")
    parser.add_argument("--watch", action="store_true", help="Watch mode: continuously classify new images")
    parser.add_argument("--reprocess", action="store_true", help="Re-classify images from classified/skipped dirs")
    parser.add_argument("--summary", action="store_true", help="Print classification summary")
    args = parser.parse_args()

    setup_logging()

    if args.summary:
        print_summary()
        return

    # Ensure directories exist
    for d in [INCOMING_DIR, CLASSIFIED_DIR, SKIPPED_DIR, FAILED_DIR, ANNOTATED_DIR, LOG_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    # Load both models
    yolo_sess, yolo_input_name = load_yolo(YOLO_MODEL_PATH)
    species_sess, species_input_name, labels = load_species_model(SPECIES_MODEL_PATH, LABELS_PATH)
    regional_species = load_regional_filter(REGIONAL_SPECIES_PATH)

    # Initialize range filter for geographic validation
    try:
        range_filter = RangeFilter()
        logging.info("Range filter loaded: geographic validation enabled")
    except Exception as e:
        logging.warning("Could not load range filter: %s — geographic filtering disabled", e)
        range_filter = None

    if args.reprocess:
        reprocess(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species, range_filter)
    elif args.watch:
        process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species, range_filter)
        watch_mode(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species, range_filter)
    else:
        n = process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species, range_filter)
        if n == 0:
            logging.info("No pending files in %s", INCOMING_DIR)
        print_summary()


if __name__ == "__main__":
    main()
