#!/usr/bin/env python3
"""
Fix missing annotated images for classified detections.
Regenerates annotated images for files that have JSONL entries but missing PNG files.
"""

import json
import logging
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# Paths
BASE_DIR = Path("/Users/vives/bird-snapshots")
JSONL_PATH = BASE_DIR / "logs" / "classifications.jsonl"
CLASSIFIED_DIR = BASE_DIR / "classified"
ANNOTATED_DIR = BASE_DIR / "annotated"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

def find_source_file(filename):
    """Find the original image in classified/species/ directories."""
    for species_dir in CLASSIFIED_DIR.iterdir():
        if species_dir.is_dir():
            candidate = species_dir / filename
            if candidate.exists():
                return candidate
    return None

def annotate_image_simple(image_path, boxes):
    """Add bounding boxes to an image without labels."""
    try:
        img = Image.open(image_path)
        draw = ImageDraw.Draw(img)

        for box in boxes:
            # box format: [x1, y1, x2, y2]
            if len(box) >= 4:
                x1, y1, x2, y2 = box[0], box[1], box[2], box[3]
                # Draw green box
                draw.rectangle([x1, y1, x2, y2], outline=(0, 255, 0), width=3)

        return img
    except Exception as e:
        logger.error(f"Failed to annotate {image_path}: {e}")
        return None

def main():
    """Regenerate missing annotations."""
    logger.info("Loading JSONL entries...")

    # Load all classified entries
    classified_entries = {}
    with open(JSONL_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("action") == "classified":
                    filename = entry.get("file")
                    if filename:
                        # Keep only the latest entry for each file
                        if filename not in classified_entries or entry.get("timestamp", "") > classified_entries[filename].get("timestamp", ""):
                            classified_entries[filename] = entry
            except json.JSONDecodeError:
                continue

    logger.info(f"Found {len(classified_entries)} classified entries")

    # Find missing annotations
    missing = []
    for filename in classified_entries:
        annotated_path = ANNOTATED_DIR / filename
        if not annotated_path.exists():
            missing.append(filename)

    logger.info(f"Found {len(missing)} missing annotations")

    # Regenerate missing annotations
    for i, filename in enumerate(missing, 1):
        entry = classified_entries[filename]
        source_file = find_source_file(filename)

        if not source_file:
            logger.warning(f"[{i}/{len(missing)}] Source file not found: {filename}")
            continue

        # Extract bounding boxes from JSONL
        boxes = []
        if entry.get("best_detection"):
            boxes.append(entry["best_detection"].get("box", []))

        # Regenerate annotation
        annotated_img = annotate_image_simple(source_file, boxes)
        if annotated_img:
            try:
                ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)
                output_path = ANNOTATED_DIR / filename
                annotated_img.save(str(output_path), quality=90)
                logger.info(f"[{i}/{len(missing)}] Regenerated: {filename}")
            except Exception as e:
                logger.error(f"[{i}/{len(missing)}] Failed to save {filename}: {e}")
        else:
            logger.error(f"[{i}/{len(missing)}] Annotation failed: {filename}")

    logger.info("Done!")

if __name__ == "__main__":
    main()
