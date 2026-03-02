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
YOLO_MODEL_PATH = MODEL_DIR / "yolov8n.onnx"
SPECIES_MODEL_PATH = MODEL_DIR / "aiy_birds_v1.onnx"
LABELS_PATH = MODEL_DIR / "inat_bird_labels.txt"
REGIONAL_SPECIES_PATH = MODEL_DIR / "cape_cod_species.txt"

# Detection thresholds
BIRD_CLASS_ID = 14                # COCO class index for "bird"
DETECTION_CONFIDENCE = 0.3        # Min confidence to consider a YOLO detection
NMS_IOU_THRESHOLD = 0.45          # Non-max suppression overlap threshold
CROP_PAD_RATIO = 0.15             # Extra padding around detected bird (15% of box size)

# Watch mode
WATCH_INTERVAL = 10  # seconds
NIGHT_CHECK_INTERVAL = 300  # seconds (5 min) — poll interval when nighttime

# Location: Cape Cod, MA (for sunset/sunrise calculation)
LATITUDE = 41.39
LONGITUDE = -70.61
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

def load_yolo(path):
    """Load YOLOv8n ONNX model."""
    logging.info("Loading YOLO detector: %s", path)
    sess = ort.InferenceSession(str(path))
    input_name = sess.get_inputs()[0].name
    logging.info("YOLO loaded: input=%s shape=%s", input_name, sess.get_inputs()[0].shape)
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

def load_species_model(path, labels_path):
    """Load AIY Birds V1 ONNX model and labels."""
    logging.info("Loading species classifier: %s", path)
    sess = ort.InferenceSession(str(path))
    input_name = sess.get_inputs()[0].name

    with open(labels_path) as f:
        labels = [line.strip() for line in f]
    logging.info("Species model loaded: %d labels", len(labels))
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

def annotate_image(image, detections, predictions, best_idx=0):
    """
    Draw bounding boxes and species labels on a copy of the image.
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

    for i, det in enumerate(detections):
        x1, y1, x2, y2 = det["box"]
        conf = det["confidence"]
        is_best = (i == best_idx)

        # Box color: green for best detection, yellow for others
        color = (0, 255, 0) if is_best else (255, 255, 0)
        width = 3 if is_best else 2
        draw.rectangle([x1, y1, x2, y2], outline=color, width=width)

        if is_best and predictions:
            top = predictions[0]
            label = f'{top["common_name"]} ({top["raw_score"]})'
            conf_label = f'det: {conf:.0%}'

            # Draw label background
            bbox = draw.textbbox((x1, y1), label, font=font)
            pad = 4
            bg_rect = [bbox[0] - pad, bbox[1] - pad, bbox[2] + pad, bbox[3] + pad]
            draw.rectangle(bg_rect, fill=(0, 0, 0, 200))
            draw.text((x1, y1), label, fill=color, font=font)

            # Detection confidence below the species label
            cy = bg_rect[3] + 2
            draw.text((x1, cy), conf_label, fill=(200, 200, 200), font=font_small)

            # Top 3 in bottom-left corner
            y_off = img.height - 80
            for j, p in enumerate(predictions[:3]):
                line = f'#{j+1} {p["common_name"]} (raw={p["raw_score"]})'
                draw.text((10, y_off), line, fill=(255, 255, 255), font=font_small)
                y_off += 22
        else:
            draw.text((x1, y1 - 20), f'bird {conf:.0%}', fill=color, font=font_small)

    return img


# ──────────────────────────────────────────────────
# Pipeline: Detect → Crop → Classify → Organize
# ──────────────────────────────────────────────────

def extract_timestamp(filename):
    """Extract timestamp from filename like 2026-03-02_11-10-42.jpg → '2026-03-02 11:10:42'."""
    try:
        stem = filename.rsplit(".", 1)[0]
        parts = stem.split("_", 1)
        if len(parts) == 2:
            date_part, time_part = parts
            return date_part + " " + time_part.replace("-", ":")
        return stem
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


def process_file(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, image_path, regional_species=None):
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
        # No bird found — skip
        result = {
            "file": fname,
            "timestamp": datetime.now().isoformat(),
            "source_timestamp": extract_timestamp(fname),
            "action": "skipped:no_bird",
            "detect_ms": round(detect_ms, 1),
            "detections": 0,
        }
        append_result(result)
        SKIPPED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(image_path), str(SKIPPED_DIR / fname))
        logging.info("SKIP %s — no bird detected (%.0fms)", fname, detect_ms)
        return result

    # Stage 2: Classify each detected bird (use highest-confidence detection)
    best_det = max(detections, key=lambda d: d["confidence"])
    bird_crop = crop_bird(img, best_det["box"])

    t1 = time.monotonic()
    predictions, raw_predictions = classify_species(
        species_sess, species_input_name, labels, bird_crop, regional_species
    )
    classify_ms = (time.monotonic() - t1) * 1000
    total_ms = (time.monotonic() - t0) * 1000

    top = predictions[0]

    # Skip if species model says "background" or unidentified
    if top["common_name"] in ("background", "unidentified bird"):
        action = "skipped:background" if top["common_name"] == "background" else "skipped:unidentified"
        result = {
            "file": fname,
            "timestamp": datetime.now().isoformat(),
            "source_timestamp": extract_timestamp(fname),
            "action": action,
            "detect_ms": round(detect_ms, 1),
            "classify_ms": round(classify_ms, 1),
            "total_ms": round(total_ms, 1),
            "detections": len(detections),
            "best_detection": best_det,
            "raw_top3": [
                {"common_name": p["common_name"], "scientific_name": p["scientific_name"], "raw_score": p["raw_score"]}
                for p in raw_predictions
            ],
        }
        append_result(result)
        SKIPPED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(image_path), str(SKIPPED_DIR / fname))
        logging.info("SKIP %s — %s (raw top: %s, %dms)", fname, action, raw_predictions[0]["common_name"], total_ms)
        return result

    # Success: bird detected and classified
    species_dir = CLASSIFIED_DIR / sanitize_dirname(top["common_name"])
    species_dir.mkdir(parents=True, exist_ok=True)

    result = {
        "file": fname,
        "timestamp": datetime.now().isoformat(),
        "source_timestamp": extract_timestamp(fname),
        "action": "classified",
        "detect_ms": round(detect_ms, 1),
        "classify_ms": round(classify_ms, 1),
        "total_ms": round(total_ms, 1),
        "detections": len(detections),
        "best_detection": best_det,
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
            for p in predictions
        ],
        "raw_top3": [
            {"common_name": p["common_name"], "scientific_name": p["scientific_name"], "raw_score": p["raw_score"]}
            for p in raw_predictions
        ],
    }
    append_result(result)

    # Save annotated image with bounding box + label
    best_idx = detections.index(best_det)
    annotated = annotate_image(img, detections, predictions, best_idx)
    ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)
    annotated.save(str(ANNOTATED_DIR / fname), quality=90)

    shutil.move(str(image_path), str(species_dir / fname))

    raw_note = ""
    if raw_predictions[0]["common_name"] != top["common_name"]:
        raw_note = f" (raw: {raw_predictions[0]['common_name']})"
    logging.info(
        "BIRD %s → %s%s (det=%.0f%%, score=%d, %dms)",
        fname,
        top["common_name"],
        raw_note,
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


def process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species=None):
    """Process all pending files."""
    files = get_pending_files()
    if not files:
        return 0

    results = []
    for fpath in files:
        r = process_file(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, fpath, regional_species)
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


def watch_mode(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species=None):
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
            n = process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species)
            if n > 0:
                logging.info("Processed %d file(s), waiting for more...", n)
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        logging.info("Watch mode stopped")


def reprocess(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species=None):
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
    process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species)


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

    if args.reprocess:
        reprocess(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species)
    elif args.watch:
        process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species)
        watch_mode(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species)
    else:
        n = process_all(yolo_sess, yolo_input_name, species_sess, species_input_name, labels, regional_species)
        if n == 0:
            logging.info("No pending files in %s", INCOMING_DIR)
        print_summary()


if __name__ == "__main__":
    main()
