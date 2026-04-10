#!/usr/bin/env python3
"""Train a yard-specific bird classifier using Coral weight imprinting.

Uses confirmed review images to train a MobileNet model on the Coral USB
that's specialized for YOUR yard's birds. Deployed alongside AIY Birds V1
as a dual-model system — the yard model wins for common species, AIY
catches rare visitors.

Can be run from the command line or triggered via the dashboard API.
"""

import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

from PIL import Image
import numpy as np

log = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
CLASSIFIED_DIR = Path.home() / "bird-snapshots" / "classified"
CLASSIFICATIONS_DB = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
IMPRINTING_MODEL = MODELS_DIR / "mobilenet_v1_1.0_224_l2norm_quant_edgetpu.tflite"
YARD_MODEL = MODELS_DIR / "yard_model.tflite"
YARD_MODEL_PREV = MODELS_DIR / "yard_model_prev.tflite"
YARD_LABELS = MODELS_DIR / "yard_model_labels.txt"
YARD_LABELS_PREV = MODELS_DIR / "yard_model_prev_labels.txt"
TRAINING_LOCK = Path("/tmp/yard-model-training.lock")
RESULTS_FILE = Path("/tmp/yard-model-results.json")

MIN_IMAGES_PER_SPECIES = 15
IMAGE_SIZE = 224


def get_training_data():
    """Extract confirmed images organized by species from the review database.

    Returns dict: {species_name: [image_paths]}
    Handles both 'correct' verdicts and 'wrong' verdicts with correct_species.
    """
    conn = sqlite3.connect(str(CLASSIFICATIONS_DB), timeout=10)
    conn.row_factory = sqlite3.Row

    species_images = {}

    # Correct verdicts — image is in classified/{species}/
    # Feeder cam only, exclude multi-bird frames
    rows = conn.execute("""
        SELECT c.file, c.common_name, c.best_detection_json
        FROM classifications c
        JOIN reviews r ON r.file = c.file
        WHERE r.verdict = 'correct'
        AND c.common_name IS NOT NULL
        AND c.best_detection_json IS NOT NULL
        AND c.camera = 'feeder'
        AND json_array_length(c.birds_json) <= 1
    """).fetchall()

    for r in rows:
        species = r["common_name"]
        fname = r["file"]
        # Find the image file
        img_path = _find_image(fname, species)
        if img_path:
            species_images.setdefault(species, []).append({
                "path": img_path,
                "box": _parse_box(r["best_detection_json"]),
            })

    # Wrong verdicts with correction — image is in classified/{wrong_species}/
    # Feeder cam only, exclude multi-bird frames
    rows = conn.execute("""
        SELECT c.file, c.common_name, r.correct_species, c.best_detection_json
        FROM classifications c
        JOIN reviews r ON r.file = c.file
        WHERE r.verdict = 'wrong'
        AND r.correct_species IS NOT NULL
        AND r.correct_species != ''
        AND r.correct_species != 'not_a_bird'
        AND c.best_detection_json IS NOT NULL
        AND c.camera = 'feeder'
        AND json_array_length(c.birds_json) <= 1
    """).fetchall()

    for r in rows:
        correct_species = r["correct_species"]
        fname = r["file"]
        wrong_species = r["common_name"]
        # Image is under the WRONG species directory
        img_path = _find_image(fname, wrong_species)
        if img_path:
            species_images.setdefault(correct_species, []).append({
                "path": img_path,
                "box": _parse_box(r["best_detection_json"]),
            })

    conn.close()

    # Filter species with enough images
    filtered = {}
    for species, images in species_images.items():
        if len(images) >= MIN_IMAGES_PER_SPECIES:
            filtered[species] = images

    return filtered


def _find_image(fname, species):
    """Find an image file in the classified directory tree."""
    safe_species = species.replace(" ", "_").replace("'", "")
    path = CLASSIFIED_DIR / safe_species / fname
    if path.exists():
        return str(path)
    # Search all subdirectories
    for subdir in CLASSIFIED_DIR.iterdir():
        if subdir.is_dir():
            candidate = subdir / fname
            if candidate.exists():
                return str(candidate)
    return None


def _parse_box(det_json):
    """Extract bounding box from detection JSON."""
    try:
        det = json.loads(det_json)
        return det.get("box", None)
    except (json.JSONDecodeError, TypeError):
        return None


def crop_and_resize(img_path, box, size=IMAGE_SIZE):
    """Load image, crop to bird bounding box with padding, resize to 224x224."""
    img = Image.open(img_path).convert("RGB")
    w, h = img.size

    if box:
        x1, y1, x2, y2 = [int(b) for b in box]
        # Add 15% padding
        bw, bh = x2 - x1, y2 - y1
        px, py = int(bw * 0.15), int(bh * 0.15)
        x1 = max(0, x1 - px)
        y1 = max(0, y1 - py)
        x2 = min(w, x2 + px)
        y2 = min(h, y2 + py)
        if x2 > x1 and y2 > y1:
            img = img.crop((x1, y1, x2, y2))

    img = img.resize((size, size), Image.LANCZOS)
    return np.array(img, dtype=np.uint8)


def train(progress_callback=None):
    """Run the full training pipeline.

    Args:
        progress_callback: Optional function(step, total, message) for progress updates.

    Returns:
        dict with training results (species trained, accuracy, model path)
    """
    from pycoral.learn.imprinting.engine import ImprintingEngine

    def progress(step, total, msg):
        log.info("[%d/%d] %s", step, total, msg)
        if progress_callback:
            progress_callback(step, total, msg)

    results = {
        "status": "starting",
        "species": {},
        "model_path": None,
        "started": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # Step 1: Get training data
    progress(1, 5, "Collecting training images...")
    training_data = get_training_data()

    if not training_data:
        results["status"] = "failed"
        results["error"] = "No species have enough confirmed images (need 15+)"
        return results

    progress(1, 5, f"Found {len(training_data)} species with enough images")
    for species, images in training_data.items():
        results["species"][species] = {"images": len(images), "trained": False}

    # Step 2: Set training lock
    progress(2, 5, "Setting training lock...")
    TRAINING_LOCK.write_text(str(os.getpid()))

    try:
        # Step 3: Prepare images and train
        progress(3, 5, "Training on Coral USB (weight imprinting)...")

        import pickle
        import subprocess

        # Phase 1: Extract the extractor model in a subprocess
        # (ImprintingEngine grabs Edge TPU, so we do this separately)
        extract_model_script = '''
import sys
sys.path.insert(0, "{base_dir}")
from pycoral.learn.imprinting.engine import ImprintingEngine
engine = ImprintingEngine("{model}", keep_classes=False)
with open("{output}", "wb") as f:
    f.write(engine.serialize_extractor_model())
print("Extractor model saved")
'''.format(base_dir=str(BASE_DIR), model=str(IMPRINTING_MODEL), output="/tmp/yard_extractor.tflite")

        proc = subprocess.run(
            [sys.executable, "-c", extract_model_script],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Extractor export failed: {proc.stderr}")
        extractor_path = "/tmp/yard_extractor.tflite"

        # Phase 2: Extract embeddings in a subprocess (separate Edge TPU session)
        species_list = sorted(training_data.keys())
        # Save image list for subprocess
        image_list_path = "/tmp/yard_training_images.pkl"
        with open(image_list_path, "wb") as f:
            pickle.dump({
                "species_list": species_list,
                "training_data": training_data,
            }, f)

        extract_script = '''
import pickle, sys, numpy as np
from pycoral.utils.edgetpu import make_interpreter
sys.path.insert(0, "{base_dir}")
from train_yard_model import crop_and_resize

with open("{image_list}", "rb") as f:
    data = pickle.load(f)

extractor = make_interpreter("{extractor}", device="usb")
extractor.allocate_tensors()
inp = extractor.get_input_details()
out = extractor.get_output_details()

all_embeddings = {{}}
for class_id, species in enumerate(data["species_list"]):
    embeddings = []
    for img_info in data["training_data"][species]:
        try:
            arr = crop_and_resize(img_info["path"], img_info["box"])
            extractor.set_tensor(inp[0]["index"], np.expand_dims(arr, 0))
            extractor.invoke()
            emb = extractor.get_tensor(out[0]["index"]).flatten().copy()
            embeddings.append(emb)
        except Exception as e:
            print(f"  WARN: {{e}}", file=sys.stderr)
    all_embeddings[class_id] = embeddings
    print(f"  Extracted: {{species}} ({{len(embeddings)}} embeddings)", file=sys.stderr)

with open("{output}", "wb") as f:
    pickle.dump(all_embeddings, f)
'''.format(
            base_dir=str(BASE_DIR),
            image_list=image_list_path,
            extractor=extractor_path,
            output="/tmp/yard_embeddings.pkl",
        )

        log.info("  Extracting embeddings via subprocess...")
        proc = subprocess.run(
            [sys.executable, "-c", extract_script],
            capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"Embedding extraction failed: {proc.stderr}")
        if proc.stderr:
            for line in proc.stderr.strip().split("\n"):
                log.info(line)

        # Load embeddings
        with open("/tmp/yard_embeddings.pkl", "rb") as f:
            all_embeddings = pickle.load(f)

        # Phase 3: Train with embeddings (creates new engine, grabs Edge TPU)
        engine = ImprintingEngine(str(IMPRINTING_MODEL), keep_classes=False)
        for class_id, species in enumerate(species_list):
            embeddings = all_embeddings.get(class_id, [])
            for emb in embeddings:
                engine.train(emb, class_id)
            if embeddings:
                results["species"][species]["trained"] = True
                results["species"][species]["images_used"] = len(embeddings)
                log.info("  Trained: %s (%d images)", species, len(embeddings))

        # Step 4: Save model
        progress(4, 5, "Saving model...")

        # Backup previous model
        if YARD_MODEL.exists():
            YARD_MODEL.rename(YARD_MODEL_PREV)
        if YARD_LABELS.exists():
            YARD_LABELS.rename(YARD_LABELS_PREV)

        with open(str(YARD_MODEL), "wb") as f:
            f.write(engine.serialize_model())
        YARD_LABELS.write_text("\n".join(species_list) + "\n")

        results["model_path"] = str(YARD_MODEL)
        results["labels_path"] = str(YARD_LABELS)
        results["species_count"] = len(species_list)
        results["total_images"] = sum(
            r.get("images_used", 0) for r in results["species"].values()
        )

        # Step 5: Done
        progress(5, 5, "Training complete!")
        results["status"] = "complete"
        results["completed"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    except Exception as e:
        results["status"] = "failed"
        results["error"] = str(e)
        log.error("Training failed: %s", e)

    finally:
        # Remove training lock
        TRAINING_LOCK.unlink(missing_ok=True)

    # Write results for dashboard
    RESULTS_FILE.write_text(json.dumps(results, indent=2))

    return results


def main():
    """CLI entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    log.info("Bird Observatory — Yard Model Training")
    log.info("=" * 50)

    results = train()

    if results["status"] == "complete":
        log.info("")
        log.info("SUCCESS!")
        log.info("  Species trained: %d", results["species_count"])
        log.info("  Total images: %d", results["total_images"])
        log.info("  Model saved: %s", results["model_path"])
        log.info("")
        for species, info in sorted(results["species"].items()):
            status = "trained" if info.get("trained") else "skipped"
            log.info("  %s: %s (%d images)", species, status, info.get("images_used", 0))
    else:
        log.error("FAILED: %s", results.get("error", "unknown"))
        sys.exit(1)


if __name__ == "__main__":
    main()
