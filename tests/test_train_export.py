"""Tests for train_export.py — training data exporter.

Uses a mock SQLite DB in tmp_path and synthetic PIL images so no real
classified-image directories are needed.
"""

import io
import json
import sqlite3
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import train_export


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SCHEMA_CLASSIFICATIONS = """
CREATE TABLE IF NOT EXISTS classifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file            TEXT UNIQUE NOT NULL,
    camera          TEXT NOT NULL DEFAULT 'feeder',
    timestamp       TEXT NOT NULL DEFAULT '2024-01-01T00:00:00',
    best_detection_json TEXT,
    birds_json      TEXT,
    common_name     TEXT
)
"""

_SCHEMA_REVIEWS = """
CREATE TABLE IF NOT EXISTS reviews (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file            TEXT UNIQUE NOT NULL,
    verdict         TEXT NOT NULL,
    correct_species TEXT DEFAULT ''
)
"""


def _make_db(tmp_path: Path) -> Path:
    """Create a minimal classifications DB with the required tables."""
    db_path = tmp_path / "classifications.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(_SCHEMA_CLASSIFICATIONS)
    conn.execute(_SCHEMA_REVIEWS)
    conn.commit()
    conn.close()
    return db_path


def _insert(conn: sqlite3.Connection, table: str, **kwargs):
    cols = ", ".join(kwargs.keys())
    placeholders = ", ".join("?" * len(kwargs))
    conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({placeholders})", list(kwargs.values()))
    conn.commit()


def _make_image(classified_dir: Path, species: str, filename: str) -> Path:
    """Create a tiny synthetic JPEG in the classified/{species}/ directory."""
    safe = species.replace(" ", "_").replace("'", "")
    species_dir = classified_dir / safe
    species_dir.mkdir(parents=True, exist_ok=True)
    img_path = species_dir / filename
    img = Image.new("RGB", (400, 300), color=(100, 150, 200))
    img.save(str(img_path), format="JPEG")
    return img_path


def _box_json(x1=50, y1=50, x2=350, y2=250) -> str:
    return json.dumps({"box": [x1, y1, x2, y2], "score": 0.95})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def db_path(tmp_path):
    return _make_db(tmp_path)


@pytest.fixture()
def classified_dir(tmp_path):
    d = tmp_path / "classified"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Test: only feeder cam images are returned
# ---------------------------------------------------------------------------

def test_feeder_only(db_path, classified_dir):
    """Ground-cam images must be excluded from the query results."""
    conn = sqlite3.connect(str(db_path))

    _make_image(classified_dir, "Black-capped Chickadee", "feeder_bird.jpg")
    _make_image(classified_dir, "Black-capped Chickadee", "ground_bird.jpg")

    _insert(conn, "classifications",
            file="feeder_bird.jpg", camera="feeder",
            common_name="Black-capped Chickadee",
            best_detection_json=_box_json(), birds_json="[]")
    _insert(conn, "classifications",
            file="ground_bird.jpg", camera="ground",
            common_name="Black-capped Chickadee",
            best_detection_json=_box_json(), birds_json="[]")
    _insert(conn, "reviews", file="feeder_bird.jpg", verdict="correct")
    _insert(conn, "reviews", file="ground_bird.jpg", verdict="correct")
    conn.close()

    results = train_export.query_confirmed_images(db_path)
    files = {r["file"] for r in results}
    assert "feeder_bird.jpg" in files
    assert "ground_bird.jpg" not in files


# ---------------------------------------------------------------------------
# Test: multi-bird frames are excluded
# ---------------------------------------------------------------------------

def test_multi_bird_excluded(db_path, classified_dir):
    """Frames with more than one bird in birds_json must be excluded."""
    conn = sqlite3.connect(str(db_path))

    _make_image(classified_dir, "American Robin", "single.jpg")
    _make_image(classified_dir, "American Robin", "multi.jpg")

    _insert(conn, "classifications",
            file="single.jpg", camera="feeder",
            common_name="American Robin",
            best_detection_json=_box_json(), birds_json='[{"label":"American Robin"}]')
    _insert(conn, "classifications",
            file="multi.jpg", camera="feeder",
            common_name="American Robin",
            best_detection_json=_box_json(),
            birds_json='[{"label":"American Robin"},{"label":"House Sparrow"}]')
    _insert(conn, "reviews", file="single.jpg", verdict="correct")
    _insert(conn, "reviews", file="multi.jpg", verdict="correct")
    conn.close()

    results = train_export.query_confirmed_images(db_path)
    files = {r["file"] for r in results}
    assert "single.jpg" in files
    assert "multi.jpg" not in files


# ---------------------------------------------------------------------------
# Test: wrong verdict with correct_species maps to corrected species
# ---------------------------------------------------------------------------

def test_wrong_verdict_remapped(db_path, classified_dir):
    """'wrong' verdict images should be labelled with correct_species, not the
    original classification.  The image still lives in the WRONG species dir."""
    conn = sqlite3.connect(str(db_path))

    # Image is filed under "House Finch" but actually a Purple Finch
    _make_image(classified_dir, "House Finch", "misid.jpg")

    _insert(conn, "classifications",
            file="misid.jpg", camera="feeder",
            common_name="House Finch",
            best_detection_json=_box_json(), birds_json="[]")
    _insert(conn, "reviews",
            file="misid.jpg", verdict="wrong",
            correct_species="Purple Finch")
    conn.close()

    results = train_export.query_confirmed_images(db_path)
    assert len(results) == 1
    rec = results[0]
    assert rec["species"] == "Purple Finch"
    assert rec["img_dir_species"] == "House Finch"  # image lives in wrong dir


# ---------------------------------------------------------------------------
# Test: crop_and_save produces a valid JPEG of the right size
# ---------------------------------------------------------------------------

def test_crop_and_save(tmp_path):
    """crop_and_save should return valid JPEG bytes with 224x224 dimensions."""
    img = Image.new("RGB", (640, 480), color=(200, 100, 50))
    src = tmp_path / "bird.jpg"
    img.save(str(src), format="JPEG")

    box = [100, 80, 400, 350]
    jpeg_bytes = train_export.crop_and_save(src, box)

    assert isinstance(jpeg_bytes, bytes)
    assert len(jpeg_bytes) > 0

    result = Image.open(io.BytesIO(jpeg_bytes))
    assert result.format == "JPEG"
    assert result.size == (224, 224)


def test_crop_and_save_no_box(tmp_path):
    """crop_and_save with no bounding box should still produce a 224x224 JPEG."""
    img = Image.new("RGB", (300, 200), color=(10, 20, 30))
    src = tmp_path / "nobox.jpg"
    img.save(str(src), format="JPEG")

    jpeg_bytes = train_export.crop_and_save(src, box=None)
    result = Image.open(io.BytesIO(jpeg_bytes))
    assert result.size == (224, 224)


# ---------------------------------------------------------------------------
# Test: train/test split is roughly 80/20
# ---------------------------------------------------------------------------

def test_stratified_split_ratio():
    """Train/test split should be close to 80/20 for species above min threshold."""
    # Build a fake species dict with 20 items per species, 2 species
    def _fake_items(n, species):
        return [{"file": f"{species}_{i}.jpg", "species": species, "img_dir_species": species}
                for i in range(n)]

    species_images = {
        "American Robin": _fake_items(20, "American Robin"),
        "House Sparrow": _fake_items(20, "House Sparrow"),
    }

    train, test, ood = train_export.stratified_split(species_images, min_images=5)

    assert len(ood) == 0
    assert len(train) + len(test) == 40

    # Should be close to 80/20 for each species
    robin_train = sum(1 for i in train if i["species"] == "American Robin")
    robin_test = sum(1 for i in test if i["species"] == "American Robin")
    assert robin_train == 16  # 80% of 20
    assert robin_test == 4    # 20% of 20


def test_stratified_split_ood():
    """Species below min_images threshold should land in OOD, not train/test."""
    species_images = {
        "Rare Warbler": [{"file": f"w{i}.jpg", "species": "Rare Warbler",
                          "img_dir_species": "Rare Warbler"} for i in range(5)],
        "Common Sparrow": [{"file": f"s{i}.jpg", "species": "Common Sparrow",
                            "img_dir_species": "Common Sparrow"} for i in range(20)],
    }

    train, test, ood = train_export.stratified_split(species_images, min_images=15)

    ood_species = {i["species"] for i in ood}
    train_species = {i["species"] for i in train}
    test_species = {i["species"] for i in test}

    assert "Rare Warbler" in ood_species
    assert "Rare Warbler" not in train_species
    assert "Rare Warbler" not in test_species
    assert "Common Sparrow" in train_species
    assert "Common Sparrow" not in ood_species


# ---------------------------------------------------------------------------
# Test: full export pipeline produces a valid zip with manifest
# ---------------------------------------------------------------------------

def test_full_export(db_path, classified_dir, tmp_path):
    """End-to-end: export() should produce a zip with manifest.json and images."""
    conn = sqlite3.connect(str(db_path))

    # Create 20 images for "American Robin" (enough for train/test split)
    for i in range(20):
        fname = f"robin_{i:03d}.jpg"
        _make_image(classified_dir, "American Robin", fname)
        _insert(conn, "classifications",
                file=fname, camera="feeder",
                common_name="American Robin",
                best_detection_json=_box_json(), birds_json="[]")
        _insert(conn, "reviews", file=fname, verdict="correct")

    # Create 5 images for "Rare Warbler" (below min-images → OOD)
    for i in range(5):
        fname = f"warbler_{i:03d}.jpg"
        _make_image(classified_dir, "Rare Warbler", fname)
        _insert(conn, "classifications",
                file=fname, camera="feeder",
                common_name="Rare Warbler",
                best_detection_json=_box_json(), birds_json="[]")
        _insert(conn, "reviews", file=fname, verdict="correct")

    conn.close()

    output_dir = tmp_path / "exports"
    zip_path = train_export.export(
        db_path=db_path,
        output_dir=output_dir,
        min_images=15,
        classified_dir=classified_dir,
        seed=42,
    )

    assert zip_path.exists()
    assert zip_path.suffix == ".zip"

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names

        # Manifest should be valid JSON
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["totals"]["train"] > 0
        assert manifest["totals"]["test"] > 0
        assert manifest["totals"]["ood"] == 5

        # Check species appear in the right splits
        sp = manifest["species"]
        assert "American Robin" in sp
        assert sp["American Robin"]["train"] == 16
        assert sp["American Robin"]["test"] == 4
        assert "Rare Warbler" in sp
        assert sp["Rare Warbler"]["ood"] == 5

        # Verify at least one image is a valid 224x224 JPEG
        image_entries = [n for n in names if n.endswith(".jpg")]
        assert len(image_entries) == 25  # 20 robin + 5 warbler

        sample = zf.read(image_entries[0])
        img = Image.open(io.BytesIO(sample))
        assert img.size == (224, 224)


# ---------------------------------------------------------------------------
# Test: wrong-verdict image is in OOD under CORRECT species name
# ---------------------------------------------------------------------------

def test_export_wrong_verdict_species_label(db_path, classified_dir, tmp_path):
    """Images with 'wrong' verdicts must be labelled (and filed) under the
    corrected species name in the zip, not the original misclassification."""
    conn = sqlite3.connect(str(db_path))

    # All 15 images misclassified as House Finch, corrected to Purple Finch
    for i in range(15):
        fname = f"finch_{i:03d}.jpg"
        _make_image(classified_dir, "House Finch", fname)
        _insert(conn, "classifications",
                file=fname, camera="feeder",
                common_name="House Finch",
                best_detection_json=_box_json(), birds_json="[]")
        _insert(conn, "reviews",
                file=fname, verdict="wrong",
                correct_species="Purple Finch")

    conn.close()

    output_dir = tmp_path / "exports"
    zip_path = train_export.export(
        db_path=db_path,
        output_dir=output_dir,
        min_images=15,
        classified_dir=classified_dir,
        seed=42,
    )

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("manifest.json"))

        # Must be labelled as Purple Finch, not House Finch
        assert "Purple Finch" in manifest["species"]
        assert "House Finch" not in manifest["species"]

        # Zip paths must use Purple_Finch directory
        image_entries = [n for n in names if n.endswith(".jpg")]
        assert all("Purple_Finch" in n for n in image_entries)
        assert not any("House_Finch" in n for n in image_entries)
