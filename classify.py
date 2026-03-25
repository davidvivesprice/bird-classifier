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
import os
import shutil
import sys
import time
from datetime import datetime, date, timedelta, timezone
from pathlib import Path

import numpy as np
from motion_gate import MotionGate
from PIL import Image, ImageDraw, ImageFont

# Range filtering for geographic validation
from range_filter import RangeFilter

# Shared solar calculations
from solar_utils import solar_times, is_nighttime, is_twilight_window

# Shared inference utilities (detection, classification, label parsing)
from bird_inference import (
    YOLODetector, SpeciesClassifier, normalize_species,
    parse_label, crop_bird, get_providers,
)

# Visit tracking
import visits_db as vdb

# --- Configuration ---
BASE_DIR = Path("/Users/vives/bird-snapshots")
INCOMING_DIR = BASE_DIR / "incoming"
CLASSIFIED_DIR = BASE_DIR / "classified"
SKIPPED_DIR = BASE_DIR / "skipped"
FAILED_DIR = BASE_DIR / "failed"
ANNOTATED_DIR = BASE_DIR / "annotated"
TRASH_DIR = BASE_DIR / "trash"
LOG_DIR = BASE_DIR / "logs"
MODEL_DIR = Path("/Users/vives/bird-classifier/models")
CULL_CONFIG_PATH = Path("/Users/vives/bird-classifier/config/cull_config.json")

# Models
YOLO_MODEL_PATH = MODEL_DIR / "yolov8n_bird.onnx"
SPECIES_MODEL_PATH = MODEL_DIR / "aiy_birds_v1.onnx"
SPECIES_TPU_PATH = MODEL_DIR / "aiy_birds_v1_edgetpu.tflite"
LABELS_PATH = MODEL_DIR / "inat_bird_labels.txt"
REGIONAL_SPECIES_PATH = MODEL_DIR / "chilmark_feeder_species.txt"

# Detection thresholds
BIRD_CLASS_ID = 0                 # Custom model: single class "bird"
DETECTION_CONFIDENCE = float(os.environ.get('DETECTION_CONFIDENCE', '0.3'))
NMS_IOU_THRESHOLD = 0.45          # Non-max suppression overlap threshold

# IR frame detection (auto-trash grayscale/infrared frames during twilight)
IR_SATURATION_THRESHOLD = float(os.environ.get('IR_SATURATION_THRESHOLD', '0.08'))
IR_WINDOW_MINUTES = int(os.environ.get('IR_WINDOW_MINUTES', '90'))

# Motion gate — skip frames where nothing changed since the last frame.
# Threshold 1.5% = at least 1.5% of pixels must change to count as motion.
# Fail-open: if the gate errors, the frame passes through.
_motion_gate = MotionGate(threshold_pct=1.5, resize_width=320)

# Watch mode
WATCH_INTERVAL = 10  # seconds
NIGHT_CHECK_INTERVAL = 300  # seconds (5 min) — poll interval when nighttime

# Location: Chilmark, Martha's Vineyard, MA (for sunset/sunrise calculation)
LATITUDE = 41.35
LONGITUDE = -70.74
NIGHT_OFFSET_MINUTES = 30  # keep running this many minutes after sunset

# Module-level model instances (initialised in main())
_detector = None       # type: YOLODetector
_classifier = None     # type: SpeciesClassifier


def is_infrared_frame(img):
    """Detect infrared/grayscale frames by checking mean color saturation.

    Cameras switch to IR mode in low light, producing desaturated B&W frames
    that are not useful for species classification or training.

    Returns (is_ir, mean_saturation) tuple for logging.
    """
    arr = np.array(img, dtype=np.float32)
    max_c = arr.max(axis=2)
    min_c = arr.min(axis=2)
    # Saturation = (max - min) / max, avoiding division by zero
    denom = np.where(max_c > 0, max_c, 1.0)
    saturation = (max_c - min_c) / denom
    mean_sat = float(saturation.mean())
    return mean_sat < IR_SATURATION_THRESHOLD, mean_sat


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


def load_regional_filter(path):
    """Load regional species allowlist. Returns a set of common names, or None if file missing."""
    if not path.exists():
        logging.warning("No regional filter at %s — all species allowed", path)
        return None
    with open(path) as f:
        species = {line.strip() for line in f if line.strip()}
    logging.info("Regional filter loaded: %d species", len(species))
    return species


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

        # Skip detections with invalid bounding boxes
        if x2 <= x1 or y2 <= y1:
            continue

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

            # Guard: skip label drawing if coordinates are invalid (edge-case detections)
            if bg_right <= bg_left or bg_bottom <= bg_top or x2 <= x1 or y2 <= y1:
                continue

            # Use semi-transparent black background for contrast
            if img.mode == 'RGBA':
                overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
                overlay_draw = ImageDraw.Draw(overlay)
                overlay_draw.rectangle([bg_left, bg_top, bg_right, bg_bottom], fill=(0, 0, 0, 180))
                img.paste(Image.alpha_composite(img.convert('RGBA'), overlay).convert(img.mode))
                draw = ImageDraw.Draw(img)
            else:
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


_cull_config_cache = None
_cull_config_mtime = 0.0


def load_cull_config():
    """Load cull config, caching until file changes."""
    global _cull_config_cache, _cull_config_mtime
    defaults = {"default_max_keep": 100, "species_caps": {}, "sufficient_species": []}
    if not CULL_CONFIG_PATH.exists():
        return defaults
    try:
        mt = CULL_CONFIG_PATH.stat().st_mtime
        if _cull_config_cache is not None and mt == _cull_config_mtime:
            return _cull_config_cache
        with open(CULL_CONFIG_PATH) as f:
            cfg = {**defaults, **json.load(f)}
        _cull_config_cache = cfg
        _cull_config_mtime = mt
        return cfg
    except Exception:
        return defaults


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
    """Write classification result to SQLite.

    SQLite is the sole data store. JSONL dual-write retired March 22, 2026
    after 3 days of stable SQLite operation. Historical JSONL preserved on
    disk as archive (not deleted).
    """
    from classifications_db import insert_classification
    insert_classification(result)


def _track_visit(result):
    """Create or extend a visit for this detection.

    Handles multi-bird frames: if result has a 'birds' array with multiple
    species, each species gets its own visit tracked separately.
    """
    camera = result.get("camera", "feeder")
    timestamp = result.get("source_timestamp") or result.get("timestamp", "")
    source_date = timestamp[:10] if len(timestamp) >= 10 else ""

    # Collect species to track: use the birds array if available for multi-bird support
    birds = result.get("birds", [])
    if birds and len(birds) > 1:
        # Multi-bird frame: track each species separately
        seen_species = set()
        for bird in birds:
            species = bird.get("species", "")
            if not species or species in seen_species:
                continue
            seen_species.add(species)
            confidence = bird.get("detection", {}).get("confidence", 0)
            score = bird.get("raw_score", 0)
            scientific = bird.get("scientific_name", "")
            snapshot = result.get("file", "")

            active = vdb.get_active_visit(camera, species, timestamp)
            if active:
                vdb.extend_visit(active["id"], timestamp, confidence, score, snapshot,
                                 bird_count=len(birds))
            else:
                vdb.start_visit(
                    camera=camera, species=species, scientific_name=scientific,
                    timestamp=timestamp, source_date=source_date,
                    confidence=confidence, score=score, snapshot=snapshot,
                    bird_count=len(birds),
                )
    else:
        # Single bird (or no birds array): use top_prediction
        pred = result["top_prediction"]
        species = pred["common_name"]
        confidence = result.get("best_detection", {}).get("confidence", 0)
        score = pred.get("raw_score", 0)
        snapshot = result.get("file", "")
        scientific = pred.get("scientific_name", "")
        bird_count = result.get("detections", 1)

        active = vdb.get_active_visit(camera, species, timestamp)
        if active:
            vdb.extend_visit(active["id"], timestamp, confidence, score, snapshot,
                             bird_count=bird_count)
        else:
            vdb.start_visit(
                camera=camera, species=species, scientific_name=scientific,
                timestamp=timestamp, source_date=source_date,
                confidence=confidence, score=score, snapshot=snapshot,
                bird_count=bird_count,
            )


def sanitize_dirname(name):
    """Convert a species name to a safe directory name."""
    return name.replace(" ", "_").replace("'", "").replace("/", "-")


def process_file(image_path, range_filter=None):
    """Full pipeline: detect birds → classify species → move file."""
    fname = os.path.basename(image_path)

    # Race guard: check JPEG is complete before processing.
    # sync_snapshots.sh may grab a file while the camera is still writing it.
    # A valid JPEG ends with the EOF marker 0xFF 0xD9.  If it's missing,
    # skip this file — it will be retried on the next classify cycle.
    try:
        with open(image_path, "rb") as _f:
            _f.seek(-2, 2)
            if _f.read() != b"\xff\xd9":
                logging.warning("Partial JPEG (no EOF marker), skipping for retry: %s", fname)
                return None
    except OSError:
        # File too small to seek — definitely incomplete
        logging.warning("Partial JPEG (too small), skipping for retry: %s", fname)
        return None

    try:
        img = Image.open(image_path)
        img.load()  # force full decode — catch corrupt JPEGs early
        img = img.convert("RGB")
    except Exception as e:
        logging.error("Failed to open %s: %s", fname, e)
        FAILED_DIR.mkdir(parents=True, exist_ok=True)
        shutil.move(str(image_path), str(FAILED_DIR / fname))
        return None
    _images_to_close = [img]  # track for cleanup in finally block

    # Auto-trash infrared/grayscale frames during twilight
    if is_twilight_window():
        is_ir, mean_sat = is_infrared_frame(img)
        if is_ir:
            result = {
                "file": fname,
                "timestamp": datetime.now().isoformat(),
                "source_timestamp": extract_timestamp(fname),
                "camera": extract_camera(fname),
                "action": "trashed:infrared",
                "mean_saturation": round(mean_sat, 4),
            }
            append_result(result)
            TRASH_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(image_path), str(TRASH_DIR / fname))
            logging.info("TRASH-IR %s — infrared frame (saturation=%.4f, threshold=%.4f)", fname, mean_sat, IR_SATURATION_THRESHOLD)
            img.close()
            return result

    t0 = time.monotonic()

    # Stage 1: Bird detection
    detections = _detector.detect(img)
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
        preds, raw_preds = _classifier.classify(bird_crop)
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

    # Auto-cull check: if species is marked sufficient and over cap, trash instead.
    # Wrapped in try/except so a cull failure never crashes the classifier —
    # the image still gets classified and saved normally if culling fails.
    try:
        cull_cfg = load_cull_config()
        species_name = top["common_name"]
        if species_name in cull_cfg.get("sufficient_species", []):
            cap = cull_cfg.get("species_caps", {}).get(species_name, cull_cfg["default_max_keep"])
            existing = len(list(species_dir.glob("*.jpg")))
            if existing >= cap:
                TRASH_DIR.mkdir(parents=True, exist_ok=True)
                result["action"] = "trashed:overcap"
                append_result(result)
                try:
                    shutil.move(str(image_path), str(TRASH_DIR / fname))
                except Exception as exc:
                    logging.warning("Failed to trash %s: %s", fname, exc)
                logging.info("CULL %s — %s over cap (%d/%d)", fname, species_name, existing, cap)
                return result
    except Exception as exc:
        logging.warning("Auto-cull check failed for %s, continuing with normal classification: %s", fname, exc)

    append_result(result)

    # Visit tracking
    if result.get("action") == "classified" and result.get("top_prediction"):
        try:
            _track_visit(result)
        except Exception as e:
            logging.warning("Visit tracking failed for %s: %s", result.get("file", "?"), e)

    # Save annotated image with bounding box + labels for ALL birds
    annotated = annotate_image(img, detections, all_predictions, best_idx)
    ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)
    annotated.save(str(ANNOTATED_DIR / fname), quality=90)
    annotated.close()
    # Close all tracked PIL images to prevent memory leak in watch mode
    for _img in _images_to_close:
        try:
            _img.close()
        except Exception:
            pass

    try:
        shutil.move(str(image_path), str(species_dir / fname))
    except Exception as exc:
        logging.warning("Failed to move %s to %s: %s", fname, species_dir, exc)

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
    """Get JPEG files in incoming/ ready for processing.

    Skips .tmp files and files modified in the last 2 seconds (may still
    be mid-transfer from sync_snapshots.sh).
    """
    if not INCOMING_DIR.exists():
        return []
    now = time.time()
    result = []
    for f in sorted(INCOMING_DIR.glob("*.jpg")):
        if f.name.endswith(".tmp"):
            continue
        try:
            if (now - f.stat().st_mtime) > 2.0:
                result.append(f)
        except OSError:
            pass  # file moved/deleted between glob and stat
    return result


def process_all(range_filter=None):
    """Process all pending files."""
    files = get_pending_files()
    if not files:
        return 0

    results = []
    motion_skipped = 0
    for fpath in files:
        camera = extract_camera(fpath.name)
        if not _motion_gate.has_motion(str(fpath), camera=camera):
            # No meaningful change since last frame from this camera — skip YOLO entirely.
            # Move the file to skipped/ so it doesn't pile up in incoming/.
            SKIPPED_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(fpath), str(SKIPPED_DIR / fpath.name))
            motion_skipped += 1
            continue
        try:
            r = process_file(fpath, range_filter)
            if r:
                results.append(r)
        except Exception as e:
            logging.error("Failed to process %s: %s", fpath.name, e)
            # Move bad file to trash so it doesn't block the queue
            try:
                TRASH_DIR.mkdir(parents=True, exist_ok=True)
                shutil.move(str(fpath), str(TRASH_DIR / fpath.name))
            except Exception:
                pass

    if motion_skipped:
        logging.info("Motion gate: skipped %d static frames (%.0f%% skip rate)",
                     motion_skipped, _motion_gate.skip_rate)

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


def watch_mode(range_filter=None):
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
            try:
                n = process_all(range_filter)
                if n > 0:
                    logging.info("Processed %d file(s), waiting for more...", n)
            except Exception as e:
                logging.error("process_all() crashed: %s", e, exc_info=True)
                time.sleep(30)  # back off before retrying
            time.sleep(WATCH_INTERVAL)
    except KeyboardInterrupt:
        logging.info("Watch mode stopped")


def reprocess(range_filter=None):
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
    process_all(range_filter)


def print_summary():
    """Print summary of classification results from SQLite."""
    import classifications_db as cdb
    total = cdb.count_total()
    species_count = cdb.count_species()
    if total == 0:
        print("No classification results yet.")
        return

    conn = cdb.get_conn(readonly=True)
    classified = conn.execute(
        "SELECT COUNT(*) FROM classifications WHERE action='classified'"
    ).fetchone()[0]
    skipped = conn.execute(
        "SELECT COUNT(*) FROM classifications WHERE action LIKE 'skipped%'"
    ).fetchone()[0]

    print(f"\n{'='*65}")
    print(f"Bird Classifier Summary")
    print(f"{'='*65}")
    print(f"Total processed:  {total}")
    print(f"Birds detected:   {classified}")
    print(f"No bird (skipped): {skipped}")
    print(f"Species:          {species_count}")
    print()

    rows = conn.execute(
        "SELECT common_name, COUNT(*) as cnt, "
        "ROUND(AVG(confidence)*100, 1) as avg_conf, "
        "ROUND(AVG(raw_score), 1) as avg_score "
        "FROM classifications WHERE action='classified' AND common_name IS NOT NULL "
        "GROUP BY common_name ORDER BY cnt DESC"
    ).fetchall()
    if rows:
        print(f"{'Species':<30} {'Count':>5}  {'Avg Det%':>8}  {'Avg Score':>9}")
        print(f"{'-'*30} {'-'*5}  {'-'*8}  {'-'*9}")
        for r in rows:
            print(f"{r[0]:<30} {r[1]:>5}  {r[2] or 0:>7.1f}%  {r[3] or 0:>9.1f}")
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

    # Load both models into module-level globals
    global _detector, _classifier

    _detector = YOLODetector(
        str(YOLO_MODEL_PATH),
        confidence=DETECTION_CONFIDENCE,
        iou_threshold=NMS_IOU_THRESHOLD,
    )
    logging.info("YOLO detector loaded: %s", YOLO_MODEL_PATH)

    regional_species = load_regional_filter(REGIONAL_SPECIES_PATH)
    _classifier = SpeciesClassifier(
        str(SPECIES_MODEL_PATH), str(LABELS_PATH),
        regional_species=regional_species,
        tpu_model_path=str(SPECIES_TPU_PATH) if SPECIES_TPU_PATH.exists() else None,
    )
    logging.info("Species classifier loaded: %s (backend=%s)", SPECIES_MODEL_PATH, _classifier._backend)

    # Initialize range filter for geographic validation
    try:
        range_filter = RangeFilter()
        logging.info("Range filter loaded: geographic validation enabled")
    except Exception as e:
        logging.warning("Could not load range filter: %s — geographic filtering disabled", e)
        range_filter = None

    # Crash recovery: end any stale active visits from a previous run
    try:
        vdb.end_stale_visits()
        logging.info("Ended any stale active visits from previous run")
    except Exception as e:
        logging.warning("Could not end stale visits: %s", e)

    if args.reprocess:
        reprocess(range_filter)
    elif args.watch:
        process_all(range_filter)
        watch_mode(range_filter)
    else:
        n = process_all(range_filter)
        if n == 0:
            logging.info("No pending files in %s", INCOMING_DIR)
        print_summary()


if __name__ == "__main__":
    main()
