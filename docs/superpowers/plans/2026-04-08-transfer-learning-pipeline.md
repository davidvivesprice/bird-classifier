# Transfer Learning Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reliable, repeatable pipeline to train a custom bird classifier using transfer learning in Google Colab, deploy it to the Coral Edge TPU, and validate it with a comprehensive test suite including real video playback.

**Architecture:** Three independent components: (1) a data exporter that packages confirmed feeder-cam images into a training-ready zip, (2) a Colab notebook that trains EfficientNet-Lite0, quantizes, and compiles for Edge TPU, (3) a video test harness that replays Protect clips through the full detection pipeline for end-to-end validation. The existing `yard_classifier.py` and `classify.py` dual-model architecture is unchanged — we only swap the model file.

**Tech Stack:** Python 3.9, SQLite, PIL, TensorFlow (Colab only), edgetpu_compiler (Colab only), PyAV, pycoral, ONNX Runtime

---

## File Structure

**Create:**
- `train_export.py` — data exporter: DB query → crop → split → zip
- `tests/test_train_export.py` — unit tests for data exporter
- `Bird_Observatory_Training.ipynb` — Colab training notebook (saved as `.ipynb`)
- `test_video_pipeline.py` — video playback test harness
- `tests/test_video_pipeline.py` — unit tests for video harness

**Modify:**
- None. Existing `classify.py`, `yard_classifier.py`, `bird_pipeline.py` are unchanged.

**Reference (read-only):**
- `train_yard_model.py` — existing training script (reuse `_find_image`, `_parse_box`, `crop_and_resize` patterns)
- `bird_pipeline.py:329-550` — `camera_loop()` is the reference for what the video harness must replicate
- `bird_inference.py` — `YOLODetector`, `SpeciesClassifier`, `crop_bird()` used by the video harness
- `bird_tracker.py` — `BirdTracker` for IoU tracking in video harness
- `yard_classifier.py` — `YardClassifier` for dual-model testing in video harness

---

### Task 1: Data Exporter — Core Logic

**Files:**
- Create: `train_export.py`
- Create: `tests/test_train_export.py`

This task builds the data exporter that queries the DB, crops images, splits train/test, and creates a zip. It reuses the proven SQL queries and crop logic from `train_yard_model.py`.

- [ ] **Step 1: Write the failing test for `get_training_species()`**

```python
# tests/test_train_export.py
"""Tests for training data export pipeline."""
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def mock_db(tmp_path):
    """Create a test DB with classifications and reviews tables."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE classifications (
        id INTEGER PRIMARY KEY, file TEXT, camera TEXT,
        common_name TEXT, best_detection_json TEXT,
        birds_json TEXT DEFAULT '[]',
        source_timestamp TEXT, source_date TEXT,
        action TEXT DEFAULT 'classified'
    )""")
    conn.execute("""CREATE TABLE reviews (
        id INTEGER PRIMARY KEY, file TEXT,
        verdict TEXT, correct_species TEXT DEFAULT ''
    )""")
    # Insert feeder + ground images with reviews
    conn.execute("""INSERT INTO classifications (file, camera, common_name, best_detection_json, birds_json)
        VALUES ('feeder_001.jpg', 'feeder', 'Black-capped Chickadee',
                '{"box": [100, 100, 200, 200]}', '[]')""")
    conn.execute("""INSERT INTO classifications (file, camera, common_name, best_detection_json, birds_json)
        VALUES ('feeder_002.jpg', 'feeder', 'Black-capped Chickadee',
                '{"box": [110, 110, 210, 210]}', '[]')""")
    conn.execute("""INSERT INTO classifications (file, camera, common_name, best_detection_json, birds_json)
        VALUES ('ground_003.jpg', 'ground', 'Black-capped Chickadee',
                '{"box": [50, 50, 150, 150]}', '[]')""")
    conn.execute("""INSERT INTO classifications (file, camera, common_name, best_detection_json, birds_json)
        VALUES ('feeder_004.jpg', 'feeder', 'Blue Jay',
                '{"box": [60, 60, 180, 180]}', '[]')""")
    # Reviews
    conn.execute("INSERT INTO reviews (file, verdict) VALUES ('feeder_001.jpg', 'correct')")
    conn.execute("INSERT INTO reviews (file, verdict) VALUES ('feeder_002.jpg', 'correct')")
    conn.execute("INSERT INTO reviews (file, verdict) VALUES ('ground_003.jpg', 'correct')")
    conn.execute("INSERT INTO reviews (file, verdict) VALUES ('feeder_004.jpg', 'correct')")
    conn.commit()
    conn.close()
    return db_path


def test_get_training_species_feeder_only(mock_db):
    """Only feeder cam images are returned."""
    from train_export import get_confirmed_images
    images = get_confirmed_images(db_path=mock_db)
    cameras = {img["camera"] for img in images}
    assert cameras == {"feeder"}, f"Expected only feeder, got {cameras}"


def test_get_training_species_excludes_multibird(mock_db):
    """Multi-bird frames are excluded."""
    conn = sqlite3.connect(str(mock_db))
    conn.execute("""INSERT INTO classifications (file, camera, common_name, best_detection_json, birds_json)
        VALUES ('feeder_multi.jpg', 'feeder', 'Song Sparrow',
                '{"box": [50, 50, 150, 150]}',
                '[{"common_name":"Song Sparrow"},{"common_name":"House Finch"}]')""")
    conn.execute("INSERT INTO reviews (file, verdict) VALUES ('feeder_multi.jpg', 'correct')")
    conn.commit()
    conn.close()

    from train_export import get_confirmed_images
    images = get_confirmed_images(db_path=mock_db)
    files = {img["file"] for img in images}
    assert "feeder_multi.jpg" not in files


def test_wrong_verdict_uses_correct_species(mock_db):
    """Wrong verdict with correct_species maps to the corrected species."""
    conn = sqlite3.connect(str(mock_db))
    conn.execute("""INSERT INTO classifications (file, camera, common_name, best_detection_json, birds_json)
        VALUES ('feeder_wrong.jpg', 'feeder', 'House Sparrow',
                '{"box": [50, 50, 150, 150]}', '[]')""")
    conn.execute("INSERT INTO reviews (file, verdict, correct_species) VALUES ('feeder_wrong.jpg', 'wrong', 'Song Sparrow')")
    conn.commit()
    conn.close()

    from train_export import get_confirmed_images
    images = get_confirmed_images(db_path=mock_db)
    wrong_img = [img for img in images if img["file"] == "feeder_wrong.jpg"]
    assert len(wrong_img) == 1
    assert wrong_img[0]["species"] == "Song Sparrow"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_train_export.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'train_export'`

- [ ] **Step 3: Write minimal implementation**

```python
# train_export.py
"""Export confirmed feeder-cam training data for transfer learning.

Queries the review database for confirmed classifications, crops bird regions
using bounding boxes, splits into train/test sets, and packages as a zip
ready for upload to Google Colab.

Usage:
    python train_export.py                    # Export to default location
    python train_export.py --output /path     # Export to custom location
    python train_export.py --min-images 20    # Require 20+ images per species
"""

import json
import logging
import os
import random
import shutil
import sqlite3
import time
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# Paths
BASE_DIR = Path(__file__).parent
CLASSIFIED_DIR = Path.home() / "bird-snapshots" / "classified"
DEFAULT_DB = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
DEFAULT_OUTPUT = Path.home() / "docs" / "bird-observatory" / "training-exports"
IMAGE_SIZE = 224
MIN_IMAGES_PER_SPECIES = 15


def get_confirmed_images(db_path=None, camera="feeder"):
    """Query DB for confirmed feeder-cam, single-bird images.

    Returns list of dicts: {file, species, camera, box}
    """
    db = db_path or DEFAULT_DB
    conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row

    images = []

    # Correct verdicts
    rows = conn.execute("""
        SELECT c.file, c.common_name AS species, c.camera, c.best_detection_json
        FROM classifications c
        JOIN reviews r ON r.file = c.file
        WHERE r.verdict = 'correct'
        AND c.common_name IS NOT NULL
        AND c.best_detection_json IS NOT NULL
        AND c.camera = ?
        AND json_array_length(c.birds_json) <= 1
    """, (camera,)).fetchall()

    for r in rows:
        box = _parse_box(r["best_detection_json"])
        images.append({
            "file": r["file"],
            "species": r["species"],
            "camera": r["camera"],
            "box": box,
        })

    # Wrong verdicts with correction
    rows = conn.execute("""
        SELECT c.file, c.common_name AS original, r.correct_species AS species,
               c.camera, c.best_detection_json
        FROM classifications c
        JOIN reviews r ON r.file = c.file
        WHERE r.verdict = 'wrong'
        AND r.correct_species IS NOT NULL
        AND r.correct_species != ''
        AND r.correct_species != 'not_a_bird'
        AND c.best_detection_json IS NOT NULL
        AND c.camera = ?
        AND json_array_length(c.birds_json) <= 1
    """, (camera,)).fetchall()

    for r in rows:
        box = _parse_box(r["best_detection_json"])
        images.append({
            "file": r["file"],
            "species": r["species"],
            "camera": r["camera"],
            "box": box,
            "original_species": r["original"],
        })

    conn.close()
    return images


def _parse_box(det_json):
    """Extract bounding box [x1, y1, x2, y2] from detection JSON."""
    try:
        det = json.loads(det_json)
        return det.get("box", None)
    except (json.JSONDecodeError, TypeError):
        return None


def _find_image(fname, species):
    """Find an image file in the classified directory tree."""
    safe_species = species.replace(" ", "_").replace("'", "")
    path = CLASSIFIED_DIR / safe_species / fname
    if path.exists():
        return path
    for subdir in CLASSIFIED_DIR.iterdir():
        if subdir.is_dir():
            candidate = subdir / fname
            if candidate.exists():
                return candidate
    return None


def crop_and_save(src_path, box, dest_path, size=IMAGE_SIZE):
    """Crop bird from image using bounding box with 15% padding, resize, save as JPEG."""
    img = Image.open(src_path).convert("RGB")
    w, h = img.size

    if box:
        x1, y1, x2, y2 = [int(b) for b in box]
        bw, bh = x2 - x1, y2 - y1
        px, py = int(bw * 0.15), int(bh * 0.15)
        x1 = max(0, x1 - px)
        y1 = max(0, y1 - py)
        x2 = min(w, x2 + px)
        y2 = min(h, y2 + py)
        if x2 > x1 and y2 > y1:
            img = img.crop((x1, y1, x2, y2))

    img = img.resize((size, size), Image.LANCZOS)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(dest_path), quality=95)
    img.close()


def export_training_data(output_dir=None, db_path=None, min_images=MIN_IMAGES_PER_SPECIES,
                         test_fraction=0.2, seed=42):
    """Export training data as organized directory with train/test/ood_test splits.

    Returns dict with export stats.
    """
    output = Path(output_dir or DEFAULT_OUTPUT)
    output.mkdir(parents=True, exist_ok=True)

    # Timestamp for this export
    ts = time.strftime("%Y%m%d_%H%M%S")
    export_dir = output / f"export_{ts}"
    export_dir.mkdir(parents=True, exist_ok=True)

    log.info("Exporting training data to %s", export_dir)

    # Get confirmed feeder images
    all_images = get_confirmed_images(db_path=db_path)

    # Group by species
    by_species = {}
    for img in all_images:
        by_species.setdefault(img["species"], []).append(img)

    # Split into trainable (>= min_images) and OOD (< min_images)
    trainable = {}
    ood = {}
    for species, imgs in sorted(by_species.items()):
        if len(imgs) >= min_images:
            trainable[species] = imgs
        else:
            ood[species] = imgs

    log.info("Trainable species: %d (%d images)", len(trainable),
             sum(len(v) for v in trainable.values()))
    log.info("OOD species: %d (%d images)", len(ood),
             sum(len(v) for v in ood.values()))

    rng = random.Random(seed)
    stats = {"train": {}, "test": {}, "ood": {}, "missing": 0, "exported": 0}

    # Export trainable species with train/test split
    for species, imgs in sorted(trainable.items()):
        rng.shuffle(imgs)
        split_idx = max(1, int(len(imgs) * (1 - test_fraction)))
        train_imgs = imgs[:split_idx]
        test_imgs = imgs[split_idx:]

        for subset, subset_imgs in [("train", train_imgs), ("test", test_imgs)]:
            safe_sp = species.replace(" ", "_").replace("'", "")
            dest_dir = export_dir / subset / safe_sp
            count = 0
            for img_info in subset_imgs:
                # Find source — check species dir first, then original species dir
                src = _find_image(img_info["file"], species)
                if not src and img_info.get("original_species"):
                    src = _find_image(img_info["file"], img_info["original_species"])
                if not src:
                    stats["missing"] += 1
                    continue
                dest = dest_dir / img_info["file"]
                crop_and_save(src, img_info["box"], dest)
                count += 1
                stats["exported"] += 1
            stats[subset][species] = count

    # Export OOD species
    for species, imgs in sorted(ood.items()):
        safe_sp = species.replace(" ", "_").replace("'", "")
        dest_dir = export_dir / "ood_test" / safe_sp
        count = 0
        for img_info in imgs:
            src = _find_image(img_info["file"], species)
            if not src and img_info.get("original_species"):
                src = _find_image(img_info["file"], img_info["original_species"])
            if not src:
                stats["missing"] += 1
                continue
            dest = dest_dir / img_info["file"]
            crop_and_save(src, img_info["box"], dest)
            count += 1
            stats["exported"] += 1
        stats["ood"][species] = count

    # Write manifest
    manifest = {
        "export_date": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "camera": "feeder",
        "min_images_per_species": min_images,
        "test_fraction": test_fraction,
        "seed": seed,
        "trainable_species": list(sorted(trainable.keys())),
        "ood_species": list(sorted(ood.keys())),
        "train_counts": stats["train"],
        "test_counts": stats["test"],
        "ood_counts": stats["ood"],
        "total_exported": stats["exported"],
        "total_missing": stats["missing"],
    }
    manifest_path = export_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # Create zip
    zip_path = output / f"training_data_{ts}"
    shutil.make_archive(str(zip_path), "zip", str(export_dir))
    log.info("Zip created: %s.zip (%d images, %d missing)",
             zip_path, stats["exported"], stats["missing"])

    return {
        "zip_path": str(zip_path) + ".zip",
        "export_dir": str(export_dir),
        "manifest": manifest,
    }


def main():
    """CLI entry point."""
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Export training data for bird classifier")
    parser.add_argument("--output", type=str, default=None, help="Output directory")
    parser.add_argument("--min-images", type=int, default=MIN_IMAGES_PER_SPECIES,
                        help="Minimum images per species to include in training set")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for train/test split")
    args = parser.parse_args()

    result = export_training_data(output_dir=args.output, min_images=args.min_images,
                                  seed=args.seed)

    manifest = result["manifest"]
    log.info("")
    log.info("Export complete!")
    log.info("  Zip: %s", result["zip_path"])
    log.info("  Training species: %d", len(manifest["trainable_species"]))
    log.info("  OOD species: %d", len(manifest["ood_species"]))
    log.info("  Total images: %d", manifest["total_exported"])
    log.info("")
    for sp in manifest["trainable_species"]:
        train_n = manifest["train_counts"].get(sp, 0)
        test_n = manifest["test_counts"].get(sp, 0)
        log.info("  %s: %d train, %d test", sp, train_n, test_n)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_train_export.py -v`
Expected: 3 tests PASS

- [ ] **Step 5: Run the exporter on real data (integration test)**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python train_export.py --min-images 15`
Expected: Zip created with 12+ species in train/, holdout in test/, remaining in ood_test/. Manifest printed showing counts.

- [ ] **Step 6: Commit**

```bash
git add train_export.py tests/test_train_export.py
git commit -m "feat: training data exporter for transfer learning pipeline

Exports confirmed feeder-cam images as cropped 224x224 JPEGs organized
into train/test/ood_test splits. Ready for upload to Colab notebook.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Colab Training Notebook

**Files:**
- Create: `Bird_Observatory_Training.ipynb`

This is the Google Colab notebook that does all the ML work. It's self-contained — no local dependencies beyond uploading the zip from Task 1.

- [ ] **Step 1: Create the notebook**

Create `Bird_Observatory_Training.ipynb` as a Jupyter notebook with these cells:

**Cell 1 — Welcome & Instructions (Markdown):**
```markdown
# 🐦 Bird Observatory — Train Your Yard Model

This notebook trains a custom bird classifier for YOUR feeder camera.

**What you need:**
1. A `training_data.zip` from your Bird Observatory (exported from the dashboard)
2. A Google account (you're in Colab, so you have one!)

**What you'll get:**
- A trained model file (`yard_model_edgetpu.tflite`) that runs on your Coral USB
- A labels file (`yard_model_labels.txt`)
- An accuracy report

**How long does it take?**
- About 5-10 minutes with a GPU

**Let's go! Run each cell in order (Shift+Enter), or click Runtime → Run All.**
```

**Cell 2 — Setup (Code):**
```python
# ── Step 1: Install what we need ──
# TensorFlow for training, edgetpu_compiler for making it run on your Coral USB
!pip install -q tensorflow
!apt-get -qq install -y edgetpu-compiler 2>/dev/null || echo "edgetpu_compiler will be installed below"

# If apt didn't work, download the compiler directly
import shutil
if not shutil.which("edgetpu_compiler"):
    !curl -sLO https://github.com/google-coral/edgetpu/releases/download/release-grouper/edgetpu_compiler-16.0-1.amd64.deb
    !dpkg -i edgetpu_compiler-16.0-1.amd64.deb 2>/dev/null || !apt-get -f install -y

import tensorflow as tf
print(f"✅ TensorFlow {tf.__version__} ready")
print(f"✅ GPU: {tf.config.list_physical_devices('GPU')}")
```

**Cell 3 — Upload Your Data (Code):**
```python
# ── Step 2: Upload your training data ──
# This will open a file picker — select your training_data_XXXXXX.zip
from google.colab import files
import zipfile, os, json

uploaded = files.upload()
zip_name = list(uploaded.keys())[0]
print(f"📦 Uploaded: {zip_name}")

# Unzip
with zipfile.ZipFile(zip_name, 'r') as z:
    z.extractall('data')

# Find the export directory (may be nested)
data_root = 'data'
if os.path.exists('data/manifest.json'):
    data_root = 'data'
else:
    for d in os.listdir('data'):
        if os.path.exists(f'data/{d}/manifest.json'):
            data_root = f'data/{d}'
            break

manifest = json.load(open(f'{data_root}/manifest.json'))
print(f"\n📋 Export from {manifest['export_date']}")
print(f"   Camera: {manifest['camera']}")
print(f"   Training species: {len(manifest['trainable_species'])}")
print(f"   OOD test species: {len(manifest['ood_species'])}")
print(f"   Total images: {manifest['total_exported']}")

TRAIN_DIR = f'{data_root}/train'
TEST_DIR = f'{data_root}/test'
OOD_DIR = f'{data_root}/ood_test'
```

**Cell 4 — Inspect Your Data (Code):**
```python
# ── Step 3: Let's look at what we have ──
# This shows sample images from each species so you can visually verify
import matplotlib.pyplot as plt
from PIL import Image
import random

species_list = sorted(os.listdir(TRAIN_DIR))
num_species = len(species_list)
print(f"🐦 {num_species} species to train on:\n")

fig, axes = plt.subplots(num_species, 4, figsize=(12, 3 * num_species))
if num_species == 1:
    axes = [axes]

for i, species in enumerate(species_list):
    sp_dir = os.path.join(TRAIN_DIR, species)
    images = os.listdir(sp_dir)
    train_count = len(images)
    test_count = len(os.listdir(os.path.join(TEST_DIR, species))) if os.path.exists(os.path.join(TEST_DIR, species)) else 0
    print(f"  {species.replace('_', ' ')}: {train_count} train, {test_count} test")

    # Show 4 random samples
    samples = random.sample(images, min(4, len(images)))
    for j, img_name in enumerate(samples):
        img = Image.open(os.path.join(sp_dir, img_name))
        axes[i][j].imshow(img)
        axes[i][j].axis('off')
        if j == 0:
            axes[i][j].set_title(species.replace('_', ' '), fontsize=10)

plt.tight_layout()
plt.suptitle('👀 Visual Verification — Do these look right?', y=1.01, fontsize=14)
plt.show()

print("\n⚠️  CHECKPOINT: Look at the images above.")
print("   If any species has wrong images (squirrels, wrong birds), STOP HERE.")
print("   Go back to the dashboard and fix the training data first.")
```

**Cell 5 — Build Model (Code):**
```python
# ── Step 4: Build the model ──
# We start with a model that already knows what birds look like (trained on
# 900+ bird species from iNaturalist). Then we teach it YOUR specific birds.

IMG_SIZE = 224
BATCH_SIZE = 32

# Data loading with augmentation for training
train_datagen = tf.keras.preprocessing.image.ImageDataGenerator(
    rescale=1./255,
    horizontal_flip=True,              # Birds face both directions
    brightness_range=[0.85, 1.15],     # Lighting changes through the day
    rotation_range=10,                 # Birds tilt on perches
    zoom_range=[0.85, 1.0],            # Slightly different distances
    fill_mode='nearest',
)

# No augmentation for test data — just rescale
test_datagen = tf.keras.preprocessing.image.ImageDataGenerator(rescale=1./255)

train_data = train_datagen.flow_from_directory(
    TRAIN_DIR, target_size=(IMG_SIZE, IMG_SIZE),
    batch_size=BATCH_SIZE, class_mode='categorical', shuffle=True, seed=42,
)

test_data = test_datagen.flow_from_directory(
    TEST_DIR, target_size=(IMG_SIZE, IMG_SIZE),
    batch_size=BATCH_SIZE, class_mode='categorical', shuffle=False,
)

NUM_CLASSES = train_data.num_classes
CLASS_NAMES = list(train_data.class_indices.keys())
print(f"\n✅ {NUM_CLASSES} species, {train_data.samples} training images, {test_data.samples} test images")

# Try to load the iNaturalist bird feature extractor from TF Hub
# Falls back to ImageNet-pretrained EfficientNet-Lite0 if not available
try:
    import tensorflow_hub as hub
    base_model = hub.KerasLayer(
        "https://tfhub.dev/google/imagenet/mobilenet_v2_100_224/feature_vector/5",
        trainable=False, input_shape=(IMG_SIZE, IMG_SIZE, 3)
    )
    print("✅ Using MobileNetV2 feature extractor from TF Hub")
    model = tf.keras.Sequential([
        base_model,
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(NUM_CLASSES, activation='softmax'),
    ])
except Exception as e:
    print(f"⚠️  TF Hub not available ({e}), using Keras EfficientNet-Lite0")
    base_model = tf.keras.applications.EfficientNetB0(
        include_top=False, weights='imagenet',
        input_shape=(IMG_SIZE, IMG_SIZE, 3), pooling='avg',
    )
    base_model.trainable = False
    model = tf.keras.Sequential([
        base_model,
        tf.keras.layers.Dropout(0.3),
        tf.keras.layers.Dense(NUM_CLASSES, activation='softmax'),
    ])

model.compile(
    optimizer=tf.keras.optimizers.Adam(learning_rate=0.01),
    loss='categorical_crossentropy',
    metrics=['accuracy'],
)
model.summary()
```

**Cell 6 — Train Phase 1 (Code):**
```python
# ── Step 5: Train Phase 1 — teach the classification head ──
# The base model is frozen — we only train the final layer that maps
# bird features → your specific species. This is fast.

print("🏋️ Phase 1: Training classification head (base model frozen)...")
history1 = model.fit(
    train_data,
    epochs=10,
    validation_data=test_data,
    verbose=1,
)
phase1_acc = history1.history['val_accuracy'][-1]
print(f"\n✅ Phase 1 complete — validation accuracy: {phase1_acc:.1%}")
```

**Cell 7 — Train Phase 2 (Code):**
```python
# ── Step 6: Train Phase 2 — fine-tune the deeper layers ──
# Now we unfreeze the top 20% of the base model and train with a very low
# learning rate. This lets the model adjust its bird-feature detectors
# to YOUR feeder's specific camera angle and lighting.

print("🔓 Unfreezing top 20% of base model for fine-tuning...")

# Unfreeze base model
if hasattr(model.layers[0], 'layers'):
    # Keras model — unfreeze top 20%
    base = model.layers[0]
    base.trainable = True
    num_layers = len(base.layers)
    freeze_until = int(num_layers * 0.8)
    for layer in base.layers[:freeze_until]:
        layer.trainable = False
    print(f"   {num_layers - freeze_until} of {num_layers} layers unfrozen")
else:
    # TF Hub layer — set trainable
    model.layers[0].trainable = True
    print("   Hub layer set to trainable")

# Recompile with lower learning rate
model.compile(
    optimizer=tf.keras.optimizers.Adam(learning_rate=0.0001),
    loss='categorical_crossentropy',
    metrics=['accuracy'],
)

print("\n🏋️ Phase 2: Fine-tuning (this takes a few minutes)...")
history2 = model.fit(
    train_data,
    epochs=10,
    validation_data=test_data,
    verbose=1,
)
phase2_acc = history2.history['val_accuracy'][-1]
print(f"\n✅ Phase 2 complete — validation accuracy: {phase2_acc:.1%}")
```

**Cell 8 — Evaluate In-Distribution (Code):**
```python
# ── Step 7: How accurate is it? ──
# Test on the holdout images the model never saw during training.
import numpy as np
from sklearn.metrics import classification_report, confusion_matrix
import seaborn as sns

# Predict on test set
test_data.reset()
predictions = model.predict(test_data, verbose=0)
pred_classes = np.argmax(predictions, axis=1)
true_classes = test_data.classes

# Per-species accuracy
species_names = [s.replace('_', ' ') for s in CLASS_NAMES]
report = classification_report(true_classes, pred_classes,
                                target_names=species_names, output_dict=True)
overall_acc = report['accuracy']

print(f"\n📊 Overall Accuracy: {overall_acc:.1%}")
print(f"   {'PASS ✅' if overall_acc >= 0.80 else 'FAIL ❌ (need ≥80%)'}\n")

print(classification_report(true_classes, pred_classes, target_names=species_names))

# Confusion matrix
cm = confusion_matrix(true_classes, pred_classes)
fig, ax = plt.subplots(figsize=(10, 8))
sns.heatmap(cm, annot=True, fmt='d', cmap='Blues',
            xticklabels=species_names, yticklabels=species_names, ax=ax)
ax.set_ylabel('Actual')
ax.set_xlabel('Predicted')
ax.set_title(f'Confusion Matrix — {overall_acc:.1%} accuracy')
plt.xticks(rotation=45, ha='right')
plt.tight_layout()
plt.show()

if overall_acc < 0.80:
    print("\n🛑 Accuracy below 80%. Consider:")
    print("   - Adding more training images for weak species")
    print("   - Checking for mislabeled training data")
    print("   - Training for more epochs")
```

**Cell 9 — Evaluate OOD (Code):**
```python
# ── Step 8: Does it know what it doesn't know? ──
# Feed images of species the model was NOT trained on.
# A good model should give LOW confidence (below threshold) — saying "I don't know."

ood_results = []
THRESHOLD = 0.45  # Same as yard_classifier.py YARD_THRESHOLD

if os.path.exists(OOD_DIR) and os.listdir(OOD_DIR):
    print("🔍 Testing out-of-distribution detection...\n")
    for species_dir in sorted(os.listdir(OOD_DIR)):
        sp_path = os.path.join(OOD_DIR, species_dir)
        if not os.path.isdir(sp_path):
            continue
        abstained = 0
        total = 0
        for img_name in os.listdir(sp_path):
            img_path = os.path.join(sp_path, img_name)
            try:
                img = tf.keras.preprocessing.image.load_img(img_path, target_size=(IMG_SIZE, IMG_SIZE))
                arr = tf.keras.preprocessing.image.img_to_array(img) / 255.0
                pred = model.predict(np.expand_dims(arr, 0), verbose=0)[0]
                max_conf = float(np.max(pred))
                total += 1
                if max_conf < THRESHOLD:
                    abstained += 1
            except Exception:
                continue
        if total > 0:
            rate = abstained / total
            status = "✅" if rate >= 0.95 else "⚠️"
            print(f"  {status} {species_dir.replace('_', ' ')}: {abstained}/{total} abstained ({rate:.0%})")
            ood_results.append({"species": species_dir, "abstained": abstained, "total": total, "rate": rate})

    if ood_results:
        overall_ood = sum(r["abstained"] for r in ood_results) / sum(r["total"] for r in ood_results)
        print(f"\n📊 Overall OOD abstention rate: {overall_ood:.0%}")
        print(f"   {'PASS ✅' if overall_ood >= 0.95 else 'NEEDS TUNING ⚠️'}")

        if overall_ood < 0.95:
            # Try finding a better threshold via temperature scaling
            print("\n🌡️ Calibrating confidence threshold...")
            for t in [0.50, 0.55, 0.60, 0.65, 0.70]:
                abstained = sum(1 for r in ood_results
                                for _ in range(r["total"])  # approximate
                                if True)  # recompute needed
                print(f"   Threshold {t:.2f}: (re-run with adjusted THRESHOLD to test)")
            print("   Try increasing THRESHOLD above and re-running this cell.")
else:
    print("ℹ️  No OOD test species in export. All species had enough images for training.")
    print("   OOD testing will be done on the iMac with video test suite.")
```

**Cell 10 — Quantize & Compile (Code):**
```python
# ── Step 9: Make it run on the Coral USB ──
# Two steps: (1) quantize to int8, (2) compile for Edge TPU hardware

import pathlib

# Save the Keras model first
model.save('yard_model_saved')

# Quantize with representative dataset
def representative_dataset():
    """Feed ~200 training images for quantization calibration."""
    train_data.reset()
    for i, (images, _) in enumerate(train_data):
        for img in images:
            yield [np.expand_dims(img, 0).astype(np.float32)]
        if i >= 200 // BATCH_SIZE:
            break

converter = tf.lite.TFLiteConverter.from_saved_model('yard_model_saved')
converter.optimizations = [tf.lite.Optimize.DEFAULT]
converter.representative_dataset = representative_dataset
converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
converter.inference_input_type = tf.uint8
converter.inference_output_type = tf.uint8

print("⚙️  Quantizing model (this takes a minute)...")
tflite_model = converter.convert()

tflite_path = 'yard_model_quant.tflite'
with open(tflite_path, 'wb') as f:
    f.write(tflite_model)
print(f"✅ Quantized model saved: {tflite_path} ({len(tflite_model) / 1024:.0f} KB)")

# Compile for Edge TPU
print("\n⚙️  Compiling for Coral Edge TPU...")
!edgetpu_compiler {tflite_path} -o .
edgetpu_path = tflite_path.replace('.tflite', '_edgetpu.tflite')

if os.path.exists(edgetpu_path):
    size_kb = os.path.getsize(edgetpu_path) / 1024
    print(f"✅ Edge TPU model ready: {edgetpu_path} ({size_kb:.0f} KB)")
    if size_kb > 8192:
        print("⚠️  Model larger than 8MB — may not fit in Edge TPU SRAM (slower inference)")
else:
    print("❌ Compilation failed — check the output above for errors")
```

**Cell 11 — Save Labels & Report (Code):**
```python
# ── Step 10: Package everything for download ──
# You'll download 3 files to put on your iMac

# Labels file (one species per line, sorted — matches model output order)
labels = [s.replace('_', ' ') for s in CLASS_NAMES]
with open('yard_model_labels.txt', 'w') as f:
    f.write('\n'.join(labels) + '\n')
print(f"✅ Labels: {len(labels)} species")

# Training report
training_report = {
    "trained_at": manifest['export_date'],
    "model_type": "EfficientNet-Lite0 / MobileNetV2 transfer learning",
    "species": labels,
    "num_species": len(labels),
    "total_training_images": train_data.samples,
    "total_test_images": test_data.samples,
    "phase1_val_accuracy": float(phase1_acc),
    "phase2_val_accuracy": float(phase2_acc),
    "overall_accuracy": float(overall_acc),
    "per_species_accuracy": {sp: float(report[sp]['f1-score']) for sp in species_names if sp in report},
    "confusion_matrix": cm.tolist(),
    "ood_results": ood_results,
    "confidence_threshold": THRESHOLD,
    "passed_accuracy_gate": overall_acc >= 0.80,
    "passed_ood_gate": (sum(r["abstained"] for r in ood_results) / max(1, sum(r["total"] for r in ood_results))) >= 0.95 if ood_results else None,
}

with open('training_report.json', 'w') as f:
    json.dump(training_report, f, indent=2)

print(f"\n📊 Training Report Summary:")
print(f"   Accuracy: {overall_acc:.1%} {'✅' if overall_acc >= 0.80 else '❌'}")
if ood_results:
    ood_rate = sum(r['abstained'] for r in ood_results) / sum(r['total'] for r in ood_results)
    print(f"   OOD Abstention: {ood_rate:.0%} {'✅' if ood_rate >= 0.95 else '❌'}")

# Download all 3 files
print("\n📥 Downloading files to your computer...")
files.download('yard_model_quant_edgetpu.tflite')
files.download('yard_model_labels.txt')
files.download('training_report.json')

print("\n🎉 Done! Copy these files to your iMac:")
print("   1. yard_model_quant_edgetpu.tflite → models/yard_model.tflite")
print("   2. yard_model_labels.txt → models/yard_model_labels.txt")
print("   3. training_report.json → keep for your records")
print("\n   Then restart the classifier:")
print("   launchctl unload ~/Library/LaunchAgents/com.vives.bird-classifier.plist")
print("   launchctl load ~/Library/LaunchAgents/com.vives.bird-classifier.plist")
```

- [ ] **Step 2: Verify the notebook is valid JSON**

Run: `cd /Users/vives/bird-classifier && python3 -c "import json; json.load(open('Bird_Observatory_Training.ipynb')); print('Valid notebook')"`
Expected: "Valid notebook"

- [ ] **Step 3: Commit**

```bash
git add Bird_Observatory_Training.ipynb
git commit -m "feat: Colab training notebook for transfer learning

Self-contained Google Colab notebook that trains EfficientNet-Lite0
on exported feeder-cam data. Includes visual verification, two-phase
training, holdout accuracy test, OOD detection test, quantization,
and Edge TPU compilation. Plain English comments throughout.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Video Test Harness

**Files:**
- Create: `test_video_pipeline.py`
- Create: `tests/test_video_pipeline.py`

This replays Protect video clips through the full bird pipeline (YOLO → crop → classify with both models → track) and generates a detailed report. Used for end-to-end validation when there are no live birds.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_video_pipeline.py
"""Tests for video pipeline test harness."""
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
from PIL import Image


@pytest.fixture
def fake_video_frames():
    """Generate a list of fake PIL frames for testing."""
    frames = []
    for i in range(10):
        img = Image.fromarray(np.random.randint(0, 255, (1080, 1920, 3), dtype=np.uint8))
        frames.append(img)
    return frames


def test_frame_results_structure():
    """Each processed frame should have the expected result structure."""
    from test_video_pipeline import FrameResult
    result = FrameResult(
        frame_number=1,
        timestamp_ms=33.3,
        detections=[],
        tracks=[],
    )
    assert result.frame_number == 1
    assert result.detections == []


def test_video_report_summarizes_species():
    """Report should count detections per species."""
    from test_video_pipeline import VideoReport, FrameResult, DetectionResult
    frames = [
        FrameResult(frame_number=1, timestamp_ms=0, detections=[
            DetectionResult(species="Black-capped Chickadee", confidence=0.92,
                           model_source="yard", box=[100, 100, 200, 200]),
        ], tracks=[]),
        FrameResult(frame_number=2, timestamp_ms=33, detections=[
            DetectionResult(species="Black-capped Chickadee", confidence=0.89,
                           model_source="yard", box=[105, 105, 205, 205]),
        ], tracks=[]),
    ]
    report = VideoReport(video_path="test.mp4", frames=frames)
    summary = report.species_summary()
    assert "Black-capped Chickadee" in summary
    assert summary["Black-capped Chickadee"]["count"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_video_pipeline.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'test_video_pipeline'`

- [ ] **Step 3: Write minimal implementation**

```python
# test_video_pipeline.py
"""Video test harness — replay Protect clips through the full bird pipeline.

Decodes video frames, runs YOLO detection → crop → classify (both models) →
IoU tracking, and generates a detailed per-frame report. Used for end-to-end
validation when there are no live birds at the feeder.

Usage:
    python test_video_pipeline.py video1.mp4 video2.mp4
    python test_video_pipeline.py --video-dir ~/Desktop/test-videos/
    python test_video_pipeline.py video.mp4 --output report.json
"""

import argparse
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import av
import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
YOLO_MODEL = BASE_DIR / "models" / "yolov8n_bird.onnx"
SPECIES_MODEL = BASE_DIR / "models" / "aiy_birds_V1_edgetpu.tflite"
LABELS = BASE_DIR / "models" / "inat_bird_labels.txt"
YARD_MODEL = BASE_DIR / "models" / "yard_model.tflite"
YARD_LABELS = BASE_DIR / "models" / "yard_model_labels.txt"
REGIONAL_SPECIES = BASE_DIR / "models" / "chilmark_feeder_species.txt"


@dataclass
class DetectionResult:
    species: str
    confidence: float
    model_source: str  # "yard", "aiy", "both_agree", "aiy_only"
    box: list


@dataclass
class FrameResult:
    frame_number: int
    timestamp_ms: float
    detections: list  # list of DetectionResult
    tracks: list  # list of track dicts from BirdTracker


@dataclass
class VideoReport:
    video_path: str
    frames: list  # list of FrameResult
    total_frames: int = 0
    fps: float = 0.0
    duration_s: float = 0.0
    processing_time_s: float = 0.0

    def species_summary(self):
        """Count detections per species across all frames."""
        counts = {}
        for frame in self.frames:
            for det in frame.detections:
                if det.species not in counts:
                    counts[det.species] = {"count": 0, "avg_confidence": 0.0,
                                           "model_sources": {}}
                counts[det.species]["count"] += 1
                counts[det.species]["avg_confidence"] += det.confidence
                src = det.model_source
                counts[det.species]["model_sources"][src] = (
                    counts[det.species]["model_sources"].get(src, 0) + 1
                )
        for sp in counts:
            if counts[sp]["count"] > 0:
                counts[sp]["avg_confidence"] /= counts[sp]["count"]
        return counts

    def to_dict(self):
        """Serialize report for JSON output."""
        return {
            "video_path": self.video_path,
            "total_frames": self.total_frames,
            "fps": self.fps,
            "duration_s": self.duration_s,
            "processing_time_s": self.processing_time_s,
            "species_summary": self.species_summary(),
            "frames_with_detections": sum(1 for f in self.frames if f.detections),
            "total_detections": sum(len(f.detections) for f in self.frames),
        }


def decode_video(video_path):
    """Decode video file into PIL Image frames using PyAV."""
    container = av.open(str(video_path))
    stream = container.streams.video[0]
    fps = float(stream.average_rate or 30)

    for frame in container.decode(video=0):
        pil_image = frame.to_image().convert("RGB")
        timestamp_ms = float(frame.pts * stream.time_base * 1000) if frame.pts else 0
        yield pil_image, timestamp_ms, fps

    container.close()


def process_video(video_path, skip_frames=2):
    """Run the full detection pipeline on a video file.

    Args:
        video_path: Path to video file
        skip_frames: Process every Nth frame (default 2 = ~15fps from 30fps source)

    Returns:
        VideoReport with per-frame results
    """
    from bird_inference import YOLODetector, SpeciesClassifier, crop_bird

    # Load models
    regional = None
    if REGIONAL_SPECIES.exists():
        regional = set(REGIONAL_SPECIES.read_text().strip().split("\n"))
        regional.discard("background")

    detector = YOLODetector(str(YOLO_MODEL), confidence=0.3)
    classifier = SpeciesClassifier(str(SPECIES_MODEL), str(LABELS),
                                    regional_species=regional)

    # Try loading yard classifier
    yard = None
    try:
        from yard_classifier import YardClassifier
        yard = YardClassifier()
        if not yard.enabled:
            yard = None
            log.info("Yard model not available — testing AIY only")
        else:
            log.info("Yard model loaded — testing dual-model classification")
    except Exception as e:
        log.info("Yard model not loaded: %s", e)

    # Optional tracker
    try:
        from bird_tracker import BirdTracker
        tracker = BirdTracker(iou_threshold=0.15, expire_seconds=5.0)
    except ImportError:
        tracker = None

    frame_results = []
    start_time = time.monotonic()
    total_frames = 0
    video_fps = 30.0

    log.info("Processing video: %s", video_path)

    for frame_num, (pil_image, ts_ms, fps) in enumerate(decode_video(video_path)):
        video_fps = fps
        total_frames += 1

        # Skip frames for speed
        if frame_num % skip_frames != 0:
            pil_image.close()
            continue

        # YOLO detection
        try:
            detections = detector.detect(pil_image)
        except Exception as e:
            log.warning("Frame %d: YOLO error: %s", frame_num, e)
            pil_image.close()
            continue

        frame_dets = []
        for det in detections:
            # Crop and classify
            crop = crop_bird(pil_image, det["box"])
            if isinstance(crop, np.ndarray):
                crop_pil = Image.fromarray(crop)
            else:
                crop_pil = crop

            if crop_pil.size[0] < 5 or crop_pil.size[1] < 5:
                continue

            # AIY classification
            try:
                filtered, _raw = classifier.classify(crop_pil)
                aiy_top = filtered[0]
                aiy_species = aiy_top["common_name"]
                aiy_conf = aiy_top.get("raw_score", 0)
            except Exception:
                aiy_species = "unknown"
                aiy_conf = 0

            # Yard model classification (if available)
            model_source = "aiy_only"
            final_species = aiy_species

            if yard:
                try:
                    yard_result = yard.classify(crop_pil)
                    if yard_result and yard_result.get("confidence", 0) >= 0.45:
                        model_source = "yard"
                        final_species = yard_result["species"]
                    else:
                        model_source = "aiy"
                except Exception:
                    model_source = "aiy"

            frame_dets.append(DetectionResult(
                species=final_species,
                confidence=det["confidence"],
                model_source=model_source,
                box=det["box"],
            ))

            if isinstance(crop, np.ndarray):
                crop_pil.close()

        # Update tracker
        track_state = []
        if tracker and detections:
            species_list = [d.species for d in frame_dets]
            trust_levels = ["normal"] * len(frame_dets)
            tracker.update(detections, species_list, trust_levels, pil_image)
            track_state = tracker.get_active_tracks()

        if frame_dets:
            frame_results.append(FrameResult(
                frame_number=frame_num,
                timestamp_ms=ts_ms,
                detections=frame_dets,
                tracks=track_state,
            ))

        pil_image.close()

    elapsed = time.monotonic() - start_time

    report = VideoReport(
        video_path=str(video_path),
        frames=frame_results,
        total_frames=total_frames,
        fps=video_fps,
        duration_s=total_frames / video_fps if video_fps > 0 else 0,
        processing_time_s=elapsed,
    )

    # Print summary
    summary = report.species_summary()
    log.info("")
    log.info("=== Video Test Report: %s ===", Path(video_path).name)
    log.info("Duration: %.1fs (%d frames at %.0f fps)", report.duration_s,
             report.total_frames, report.fps)
    log.info("Processing time: %.1fs (%.1fx realtime)",
             elapsed, report.duration_s / elapsed if elapsed > 0 else 0)
    log.info("Frames with detections: %d / %d",
             sum(1 for f in frame_results if f.detections),
             total_frames // skip_frames)
    log.info("")
    if summary:
        log.info("Species detected:")
        for sp, data in sorted(summary.items()):
            sources = ", ".join(f"{k}={v}" for k, v in data["model_sources"].items())
            log.info("  %s: %d detections (avg conf %.0f%%, %s)",
                     sp, data["count"], data["avg_confidence"] * 100, sources)
    else:
        log.info("No birds detected in this video.")

    return report


def main():
    """CLI entry point."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    parser = argparse.ArgumentParser(description="Video test harness for bird pipeline")
    parser.add_argument("videos", nargs="*", help="Video files to process")
    parser.add_argument("--video-dir", type=str, help="Directory of video files")
    parser.add_argument("--output", type=str, help="Save report as JSON")
    parser.add_argument("--skip-frames", type=int, default=2,
                        help="Process every Nth frame (default: 2)")
    args = parser.parse_args()

    video_files = []
    if args.videos:
        video_files.extend(args.videos)
    if args.video_dir:
        vdir = Path(args.video_dir)
        video_files.extend(str(f) for f in vdir.glob("*.mp4"))
        video_files.extend(str(f) for f in vdir.glob("*.mov"))

    if not video_files:
        parser.error("No video files specified. Use positional args or --video-dir")

    all_reports = []
    for vf in video_files:
        report = process_video(vf, skip_frames=args.skip_frames)
        all_reports.append(report)

    if args.output:
        output_data = [r.to_dict() for r in all_reports]
        Path(args.output).write_text(json.dumps(output_data, indent=2))
        log.info("Report saved to %s", args.output)


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_video_pipeline.py -v`
Expected: 2 tests PASS

- [ ] **Step 5: Commit**

```bash
git add test_video_pipeline.py tests/test_video_pipeline.py
git commit -m "feat: video test harness for end-to-end pipeline validation

Replays Protect video clips through full detection pipeline: YOLO →
crop → classify (dual model) → IoU tracking. Generates per-frame
timeline and species summary report. Used for regression testing
when no live birds are available.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Integration Test — Full Export + Deploy Cycle

**Files:**
- Modify: none (integration test only)

This task verifies the full pipeline works end-to-end on real data.

- [ ] **Step 1: Run the data exporter on real data**

Run:
```bash
cd /Users/vives/bird-classifier
/Users/vives/bird-classifier/venv-coral/bin/python train_export.py --min-images 15
```

Expected: A zip file created at `~/docs/bird-observatory/training-exports/training_data_XXXXXX.zip` with `train/`, `test/`, `ood_test/` directories and `manifest.json`.

- [ ] **Step 2: Verify the zip structure**

Run:
```bash
LATEST=$(ls -t ~/docs/bird-observatory/training-exports/training_data_*.zip | head -1)
echo "Zip: $LATEST"
python3 -c "
import zipfile, json
z = zipfile.ZipFile('$LATEST')
dirs = set()
for f in z.namelist():
    parts = f.split('/')
    if len(parts) >= 2:
        dirs.add(parts[0] + '/' + parts[1] if parts[1] else parts[0])
print('Top-level dirs:', sorted(dirs)[:20])
# Check manifest
manifest = json.loads(z.read([n for n in z.namelist() if n.endswith('manifest.json')][0]))
print('Species:', len(manifest['trainable_species']))
print('OOD:', len(manifest['ood_species']))
print('Total:', manifest['total_exported'])
"
```

Expected: train/{species}/, test/{species}/, ood_test/{species}/ directories. 12+ trainable species, OOD species present, 800+ total images.

- [ ] **Step 3: Test video harness with a short clip (when David provides videos)**

Run:
```bash
cd /Users/vives/bird-classifier
/Users/vives/bird-classifier/venv-coral/bin/python test_video_pipeline.py ~/Desktop/test-video.mp4 --output /tmp/video-test-report.json
```

Expected: Species detected with model source attribution. Report saved as JSON.

- [ ] **Step 4: Commit all outstanding changes**

```bash
git add -A
git commit -m "feat: transfer learning pipeline — complete implementation

Three components ready:
1. train_export.py — data exporter (DB → cropped images → zip)
2. Bird_Observatory_Training.ipynb — Colab notebook (train → quantize → compile)
3. test_video_pipeline.py — video test harness (replay → detect → report)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```
