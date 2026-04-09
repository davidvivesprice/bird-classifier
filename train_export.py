#!/usr/bin/env python3
"""Export confirmed feeder-cam images as a training zip for transfer learning.

Queries the classifications DB for confirmed single-bird feeder images, crops
each to its bounding box (with 15% padding), resizes to 224x224, splits into
80/20 train/test sets (stratified by species), and packages everything as a zip
with a manifest.json. Species with fewer than --min-images samples are placed
in a separate OOD (out-of-distribution) test set instead.

Output zip structure:
    train/<Species_Name>/<filename>.jpg
    test/<Species_Name>/<filename>.jpg
    ood/<Species_Name>/<filename>.jpg  (species below min-images threshold)
    manifest.json

Usage:
    python train_export.py [--min-images N] [--output PATH] [--db PATH]
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import random
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

# Defaults
DEFAULT_DB = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
DEFAULT_OUTPUT_DIR = Path.home() / "docs" / "bird-observatory" / "training-exports"
CLASSIFIED_DIR = Path.home() / "bird-snapshots" / "classified"
IMAGE_SIZE = 224
DEFAULT_MIN_IMAGES = 15
TRAIN_RATIO = 0.8


# ---------------------------------------------------------------------------
# Database queries
# ---------------------------------------------------------------------------

def query_confirmed_images(db_path: Path) -> list[dict]:
    """Return all confirmed feeder-cam single-bird images from the DB.

    Each entry is a dict with: file, species, box (or None), img_dir_species.
    img_dir_species is the species directory where the image actually lives
    (differs from species for 'wrong' verdicts).
    """
    conn = sqlite3.connect(str(db_path), timeout=10)
    conn.row_factory = sqlite3.Row
    results = []

    # --- Correct verdicts ---
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

    for row in rows:
        results.append({
            "file": row["file"],
            "species": row["common_name"],
            "img_dir_species": row["common_name"],
            "box": _parse_box(row["best_detection_json"]),
        })

    # --- Wrong verdicts with correction ---
    rows = conn.execute("""
        SELECT c.file, c.common_name AS wrong_species, r.correct_species,
               c.best_detection_json
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

    for row in rows:
        results.append({
            "file": row["file"],
            "species": row["correct_species"],
            "img_dir_species": row["wrong_species"],  # image lives under wrong dir
            "box": _parse_box(row["best_detection_json"]),
        })

    conn.close()
    return results


def _parse_box(det_json: str) -> list | None:
    """Extract [x1, y1, x2, y2] bounding box from detection JSON."""
    try:
        det = json.loads(det_json)
        return det.get("box", None)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def _find_image(fname: str, species: str, classified_dir: Path = CLASSIFIED_DIR) -> Path | None:
    """Locate an image in the classified directory tree.

    Tries the expected species subdirectory first, then falls back to a full
    scan so renamed or moved files are still found.
    """
    safe = species.replace(" ", "_").replace("'", "")
    candidate = classified_dir / safe / fname
    if candidate.exists():
        return candidate
    # Fallback: search all subdirs
    for subdir in classified_dir.iterdir():
        if subdir.is_dir():
            p = subdir / fname
            if p.exists():
                return p
    return None


def crop_and_save(img_path: Path, box: list | None, size: int = IMAGE_SIZE) -> bytes:
    """Load, crop (with 15% padding), resize, and return JPEG bytes."""
    img = Image.open(img_path).convert("RGB")
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

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Split logic
# ---------------------------------------------------------------------------

def stratified_split(
    species_images: dict[str, list[dict]],
    min_images: int = DEFAULT_MIN_IMAGES,
    train_ratio: float = TRAIN_RATIO,
    seed: int = 42,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Split images into train, test, and OOD sets.

    Species with >= min_images go into train/test (stratified 80/20).
    Species with < min_images go entirely into the OOD set.

    Returns (train_items, test_items, ood_items).
    Each item is a dict with 'species' and image metadata.
    """
    rng = random.Random(seed)
    train, test, ood = [], [], []

    for species, items in species_images.items():
        if len(items) < min_images:
            for item in items:
                ood.append({**item, "split": "ood"})
            continue

        shuffled = list(items)
        rng.shuffle(shuffled)
        n_train = max(1, int(len(shuffled) * train_ratio))
        for item in shuffled[:n_train]:
            train.append({**item, "split": "train"})
        for item in shuffled[n_train:]:
            test.append({**item, "split": "test"})

    return train, test, ood


# ---------------------------------------------------------------------------
# Main export
# ---------------------------------------------------------------------------

def export(
    db_path: Path = DEFAULT_DB,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    min_images: int = DEFAULT_MIN_IMAGES,
    classified_dir: Path = CLASSIFIED_DIR,
    seed: int = 42,
) -> Path:
    """Run the full export pipeline and return the path to the output zip."""
    log.info("Bird Observatory — Training Data Export")
    log.info("=" * 50)
    log.info("DB:            %s", db_path)
    log.info("Classified:    %s", classified_dir)
    log.info("Output dir:    %s", output_dir)
    log.info("Min images:    %d", min_images)

    # 1. Query DB
    log.info("Querying confirmed feeder images...")
    raw = query_confirmed_images(db_path)
    log.info("  %d candidate records", len(raw))

    # 2. Resolve image paths, group by species
    species_images: dict[str, list[dict]] = {}
    missing = 0
    for rec in raw:
        img_path = _find_image(rec["file"], rec["img_dir_species"], classified_dir)
        if img_path is None:
            log.debug("  Missing: %s (under %s)", rec["file"], rec["img_dir_species"])
            missing += 1
            continue
        rec["img_path"] = img_path
        species_images.setdefault(rec["species"], []).append(rec)

    log.info("  %d images found, %d missing on disk", len(raw) - missing, missing)
    log.info("  %d distinct species", len(species_images))

    # 3. Split
    train_items, test_items, ood_items = stratified_split(
        species_images, min_images=min_images, train_ratio=TRAIN_RATIO, seed=seed
    )
    log.info(
        "  Train: %d | Test: %d | OOD: %d",
        len(train_items), len(test_items), len(ood_items),
    )

    if not train_items and not ood_items:
        raise RuntimeError(
            f"No exportable images found. Check DB path and that feeder images exist "
            f"with confirmed reviews."
        )

    # 4. Build zip
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    zip_path = output_dir / f"training_export_{ts}.zip"

    manifest = {
        "created": datetime.now().isoformat(),
        "camera": "feeder",
        "db_path": str(db_path),
        "min_images_per_species": min_images,
        "image_size": IMAGE_SIZE,
        "train_ratio": TRAIN_RATIO,
        "totals": {
            "train": len(train_items),
            "test": len(test_items),
            "ood": len(ood_items),
        },
        "species": {},
    }

    errors = 0
    written = 0

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        all_items = (
            [(item, "train") for item in train_items]
            + [(item, "test") for item in test_items]
            + [(item, "ood") for item in ood_items]
        )

        for item, split in all_items:
            species = item["species"]
            safe_species = species.replace(" ", "_").replace("'", "")
            img_path = item["img_path"]

            try:
                jpeg_bytes = crop_and_save(img_path, item.get("box"))
            except Exception as exc:
                log.warning("  Crop failed for %s: %s", img_path, exc)
                errors += 1
                continue

            arc_name = f"{split}/{safe_species}/{img_path.name}"
            zf.writestr(arc_name, jpeg_bytes)
            written += 1

            # Update manifest species entry
            sp_entry = manifest["species"].setdefault(species, {
                "safe_name": safe_species,
                "train": 0,
                "test": 0,
                "ood": 0,
            })
            sp_entry[split] += 1

        # Write manifest
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

    log.info("=" * 50)
    log.info("Written %d images (%d errors) → %s", written, errors, zip_path)
    log.info("Species breakdown:")
    for sp, info in sorted(manifest["species"].items()):
        log.info(
            "  %-35s  train=%d  test=%d  ood=%d",
            sp, info["train"], info["test"], info["ood"],
        )

    return zip_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export confirmed feeder-cam images for transfer learning.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help="Path to classifications.db",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Directory for the output zip file",
    )
    parser.add_argument(
        "--min-images",
        type=int,
        default=DEFAULT_MIN_IMAGES,
        dest="min_images",
        help="Minimum images per species to include in train/test (below goes to OOD)",
    )
    parser.add_argument(
        "--classified-dir",
        type=Path,
        default=CLASSIFIED_DIR,
        dest="classified_dir",
        help="Root directory containing classified/{Species_Name}/ subdirs",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible train/test split",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser


def main():
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    try:
        zip_path = export(
            db_path=args.db,
            output_dir=args.output,
            min_images=args.min_images,
            classified_dir=args.classified_dir,
            seed=args.seed,
        )
        print(zip_path)
    except Exception as exc:
        log.error("Export failed: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
