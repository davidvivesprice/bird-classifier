#!/usr/bin/env python3
"""
Export reviewed bird images to YOLO training format.

Reads human-reviewed classifications and converts them to a YOLO dataset
for fine-tuning YOLOv8n as a feeder-specific bird detector.

Positive examples: reviewed "correct" + "wrong" (bird is there, box is valid)
Hard negatives: reviewed "trash" (YOLO false positives — not actually birds)
Easy negatives: random "no_bird" frames from recent captures

Output: dataset/ directory ready for Ultralytics YOLO training
"""

import json
import os
import random
import shutil
from pathlib import Path

# ── Paths ──
BASE_DIR = Path("/Users/vives/bird-snapshots")
CLASSIFIED_DIR = BASE_DIR / "classified"
TRASH_DIR = BASE_DIR / "trash"
INCOMING_DIR = BASE_DIR / "incoming"
JSONL_PATH = BASE_DIR / "logs" / "classifications.jsonl"
REVIEWS_PATH = Path("/Users/vives/bird-classifier/dashboard/reviews.jsonl")
DATASET_DIR = Path("/Users/vives/bird-classifier/dataset")

# Image dimensions (all captures are 1920x1080)
IMG_W = 1920
IMG_H = 1080

# Dataset split
VAL_RATIO = 0.2
NUM_EASY_NEGATIVES = 100  # random no_bird frames

random.seed(42)


def load_reviews():
    """Load review verdicts, keeping last verdict per file."""
    reviews = {}
    with open(REVIEWS_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                reviews[r["file"]] = r
    return reviews


def load_classifications():
    """Load JSONL, return dict of file -> entry (last entry wins)."""
    entries = {}
    with open(JSONL_PATH) as f:
        for line in f:
            line = line.strip()
            if line:
                e = json.loads(line)
                entries[e["file"]] = e
    return entries


def find_image(filename):
    """Find an image in classified/ subdirectories."""
    for species_dir in CLASSIFIED_DIR.iterdir():
        if not species_dir.is_dir():
            continue
        img_path = species_dir / filename
        if img_path.exists():
            return img_path
    return None


def find_recent_no_bird_frames(classifications, n=100):
    """Find recent no_bird frames that still exist on disk."""
    # Collect no_bird entries, newest first
    no_bird = []
    for fname, e in classifications.items():
        if e.get("action") in ("no_bird", "skipped:no_bird"):
            no_bird.append(e)

    # Sort by timestamp descending
    no_bird.sort(key=lambda x: x.get("source_timestamp", ""), reverse=True)

    # Find ones that still exist (check incoming/ and capture directories)
    # Since classifier deletes no_bird frames, we'll capture fresh ones
    # For now, use what we can find
    found = []
    capture_dirs = [
        BASE_DIR / "incoming",
        Path("/Users/vives/bird-snapshots"),
    ]

    # Most no_bird frames are deleted, so we'll grab from the capture stream
    # by saving some current frames as negatives
    return found  # May be empty — that's OK, we have trash/ as hard negatives


def convert_box_to_yolo(box):
    """Convert [x_min, y_min, x_max, y_max] pixel coords to YOLO normalized format."""
    x_min, y_min, x_max, y_max = box
    x_center = (x_min + x_max) / 2.0 / IMG_W
    y_center = (y_min + y_max) / 2.0 / IMG_H
    width = (x_max - x_min) / IMG_W
    height = (y_max - y_min) / IMG_H

    # Clamp to [0, 1]
    x_center = max(0.0, min(1.0, x_center))
    y_center = max(0.0, min(1.0, y_center))
    width = max(0.0, min(1.0, width))
    height = max(0.0, min(1.0, height))

    return x_center, y_center, width, height


def main():
    print("Loading reviews...")
    reviews = load_reviews()
    print(f"  {len(reviews)} reviews loaded")

    print("Loading classifications...")
    classifications = load_classifications()
    print(f"  {len(classifications)} entries loaded")

    # ── Collect positive examples ──
    positives = []
    for fname, review in reviews.items():
        if review["verdict"] not in ("correct", "wrong"):
            continue

        entry = classifications.get(fname)
        if not entry or entry.get("action") != "classified":
            continue

        box = entry.get("best_detection", {}).get("box")
        if not box:
            continue

        img_path = find_image(fname)
        if not img_path:
            continue

        # Also check for multi-bird detections — include ALL boxes
        all_boxes = []
        if "birds" in entry:
            for bird in entry["birds"]:
                if "box" in bird:
                    all_boxes.append(bird["box"])
        if not all_boxes:
            all_boxes = [box]

        positives.append({
            "file": fname,
            "img_path": img_path,
            "boxes": all_boxes,
        })

    print(f"  {len(positives)} positive images (reviewed correct/wrong with boxes)")

    # ── Collect manually verified negatives ──
    # These are frames manually checked to have NO birds in them
    negatives_dir = Path("/Users/vives/bird-classifier/dataset_negatives")
    all_negatives = []
    if negatives_dir.exists():
        for img_file in sorted(negatives_dir.iterdir()):
            if img_file.suffix.lower() in (".jpg", ".jpeg", ".png"):
                all_negatives.append({
                    "file": img_file.name,
                    "img_path": img_file,
                })

    print(f"  {len(all_negatives)} verified negatives (manually checked, no birds)")
    total = len(positives) + len(all_negatives)
    print(f"\nTotal dataset: {total} images ({len(positives)} positive, {len(all_negatives)} negative)")

    if len(positives) == 0:
        print("ERROR: No positive examples found. Aborting.")
        return

    # ── Split into train/val ──
    random.shuffle(positives)
    random.shuffle(all_negatives)

    val_pos = int(len(positives) * VAL_RATIO)
    val_neg = int(len(all_negatives) * VAL_RATIO)

    splits = {
        "train": positives[val_pos:] + all_negatives[val_neg:],
        "val": positives[:val_pos] + all_negatives[:val_neg],
    }

    # ── Create dataset directory ──
    if DATASET_DIR.exists():
        shutil.rmtree(DATASET_DIR)

    for split_name in ("train", "val"):
        (DATASET_DIR / "images" / split_name).mkdir(parents=True)
        (DATASET_DIR / "labels" / split_name).mkdir(parents=True)

    # ── Export images and labels ──
    stats = {"train": {"pos": 0, "neg": 0}, "val": {"pos": 0, "neg": 0}}

    for split_name, items in splits.items():
        for item in items:
            img_src = item["img_path"]
            img_dst = DATASET_DIR / "images" / split_name / item["file"]
            label_dst = DATASET_DIR / "labels" / split_name / (Path(item["file"]).stem + ".txt")

            # Copy image
            shutil.copy2(img_src, img_dst)

            if "boxes" in item:
                # Positive example — write bounding box labels
                with open(label_dst, "w") as f:
                    for box in item["boxes"]:
                        xc, yc, w, h = convert_box_to_yolo(box)
                        f.write(f"0 {xc:.6f} {yc:.6f} {w:.6f} {h:.6f}\n")
                stats[split_name]["pos"] += 1
            else:
                # Negative example — empty label file
                label_dst.touch()
                stats[split_name]["neg"] += 1

    # ── Write dataset.yaml ──
    yaml_content = f"""path: {DATASET_DIR}
train: images/train
val: images/val
nc: 1
names:
  0: bird
"""
    with open(DATASET_DIR / "dataset.yaml", "w") as f:
        f.write(yaml_content)

    # ── Summary ──
    print(f"\n{'='*50}")
    print(f"Dataset exported to: {DATASET_DIR}")
    print(f"  Train: {stats['train']['pos']} positive + {stats['train']['neg']} negative = {stats['train']['pos'] + stats['train']['neg']}")
    print(f"  Val:   {stats['val']['pos']} positive + {stats['val']['neg']} negative = {stats['val']['pos'] + stats['val']['neg']}")
    print(f"\nYAML: {DATASET_DIR / 'dataset.yaml'}")
    print(f"\nNext: zip -r dataset.zip dataset/")
    print(f"Then upload to Google Colab for training.")

    # ── Spot-check ──
    print(f"\n{'='*50}")
    print("Spot-check (first 3 positive labels):")
    label_dir = DATASET_DIR / "labels" / "train"
    count = 0
    for lbl in sorted(label_dir.iterdir()):
        if lbl.stat().st_size > 0 and count < 3:
            print(f"  {lbl.name}: {lbl.read_text().strip()}")
            count += 1


if __name__ == "__main__":
    main()
