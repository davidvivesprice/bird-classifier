#!/usr/bin/env python3
"""
Regenerate ALL annotated images with improved label placement.
This fixes the issue where labels overlap bird heads in existing images.
Uses only PIL - no ONNX dependencies.
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

def annotate_image_improved(image_path, detections, species_predictions):
    """
    Draw bounding boxes and improved labels on image.
    Replicates the improved label placement from classify.py:
    - Labels placed ABOVE/BELOW boxes, not overlaying
    - Dark background behind text for readability
    """
    try:
        img = Image.open(image_path).convert('RGB')
        draw = ImageDraw.Draw(img)

        # Try to load fonts (fallback to default if not available)
        try:
            font = ImageFont.truetype("/Library/Fonts/Arial.ttf", 14)
            font_small = ImageFont.truetype("/Library/Fonts/Arial.ttf", 11)
        except:
            font = ImageFont.load_default()
            font_small = ImageFont.load_default()

        # Draw each detection
        colors = [(0, 255, 0), (0, 255, 255), (255, 255, 0), (255, 0, 255), (0, 255, 128)]

        for idx, det in enumerate(detections):
            if not isinstance(det, dict) or 'box' not in det:
                continue

            box = det['box']
            if len(box) < 4:
                continue

            x1, y1, x2, y2 = int(box[0]), int(box[1]), int(box[2]), int(box[3])
            conf = det.get('confidence', 0)
            color = colors[idx % len(colors)]

            # Draw green bounding box
            draw.rectangle([x1, y1, x2, y2], outline=color, width=3)

            # Get species name and prediction score
            if idx < len(species_predictions) and species_predictions[idx]:
                preds = species_predictions[idx]
                if preds and isinstance(preds, list) and len(preds) > 0:
                    top = preds[0]
                    species_name = top.get('common_name', 'Unknown')
                    raw_score = top.get('raw_score', 0)
                    label = f'{species_name} ({raw_score})'
                    conf_label = f'det: {conf:.0%}'

                    # Measure text dimensions
                    label_bbox = draw.textbbox((0, 0), label, font=font)
                    label_w = label_bbox[2] - label_bbox[0]
                    label_h = label_bbox[3] - label_bbox[1]
                    conf_bbox = draw.textbbox((0, 0), conf_label, font=font_small)
                    conf_h = conf_bbox[3] - conf_bbox[1]
                    pad = 6
                    gap = 3
                    total_h = pad + label_h + gap + conf_h + pad

                    # Place label ABOVE the box if room, otherwise BELOW
                    if y1 - total_h - 5 >= 0:  # Extra 5px margin above box
                        block_top = y1 - total_h - 5
                    else:
                        block_top = y2 + 5  # Extra 5px margin below box

                    species_y = block_top + pad
                    conf_y = species_y + label_h + gap

                    # Draw dark background behind text
                    bg_left = max(0, x1 - pad)
                    bg_right = min(img.width, x1 + label_w + 2 * pad)
                    bg_top = max(0, block_top)
                    bg_bottom = min(img.height, block_top + total_h)
                    draw.rectangle([bg_left, bg_top, bg_right, bg_bottom], fill=(0, 0, 0))

                    # Draw text with contrasting color
                    draw.text((x1, species_y), label, fill=color, font=font)
                    draw.text((x1, conf_y), conf_label, fill=(200, 200, 200), font=font_small)

        return img
    except Exception as e:
        logger.error(f"Error annotating {image_path}: {e}")
        return None

def main():
    """Regenerate all annotations."""
    logger.info("Loading JSONL entries...")

    # Load all classified entries (keep latest for each file)
    classified_entries = {}
    with open(JSONL_PATH) as f:
        for line in f:
            try:
                entry = json.loads(line)
                if entry.get("action") == "classified":
                    filename = entry.get("file")
                    if filename:
                        if filename not in classified_entries or entry.get("timestamp", "") > classified_entries[filename].get("timestamp", ""):
                            classified_entries[filename] = entry
            except json.JSONDecodeError:
                continue

    files_to_regenerate = list(classified_entries.keys())
    logger.info(f"Found {len(files_to_regenerate)} classified entries to regenerate")

    success_count = 0
    fail_count = 0

    for i, filename in enumerate(files_to_regenerate, 1):
        entry = classified_entries[filename]
        source_file = find_source_file(filename)

        if not source_file:
            logger.warning(f"[{i}/{len(files_to_regenerate)}] Source file not found: {filename}")
            fail_count += 1
            continue

        try:
            # Extract detection boxes
            detections = []
            if entry.get("best_detection"):
                detections.append(entry["best_detection"])

            # Extract species predictions
            species_preds = []
            if entry.get("top3"):
                species_preds.append(entry["top3"])

            # Annotate with improved label placement
            annotated_img = annotate_image_improved(str(source_file), detections, species_preds)

            if annotated_img:
                # Save
                ANNOTATED_DIR.mkdir(parents=True, exist_ok=True)
                output_path = ANNOTATED_DIR / filename
                annotated_img.save(str(output_path), quality=90)
                success_count += 1

                if i % 200 == 0:
                    logger.info(f"[{i}/{len(files_to_regenerate)}] Progress: {i} files regenerated")
            else:
                fail_count += 1

        except Exception as e:
            logger.error(f"[{i}/{len(files_to_regenerate)}] Failed to regenerate {filename}: {e}")
            fail_count += 1

    logger.info(f"✓ Done! Regenerated {success_count}/{len(files_to_regenerate)} annotations ({fail_count} failures)")

if __name__ == "__main__":
    main()
