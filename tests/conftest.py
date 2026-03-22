"""
Shared pytest fixtures for the bird classification test suite.
"""

import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Directory / path fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def project_root() -> Path:
    """Return the Path to the project root (worktree root)."""
    return Path(__file__).parent.parent


@pytest.fixture(scope="session")
def models_dir() -> Path:
    """Return the Path to the shared models directory.

    Models live in the *original* checkout, not the worktree, because
    models/ is gitignored and therefore not replicated into the worktree.
    """
    return Path("/Users/vives/bird-classifier/models")


# ---------------------------------------------------------------------------
# Model / label file fixtures  (skip if not present)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def yolo_model_path(models_dir: Path) -> Path:
    """Path to the YOLOv8n bird detection ONNX model."""
    path = models_dir / "yolov8n_bird.onnx"
    if not path.exists():
        pytest.skip(f"YOLO model not found: {path}")
    return path


@pytest.fixture(scope="session")
def species_model_path(models_dir: Path) -> Path:
    """Path to the AIY Birds V1 species classification ONNX model."""
    path = models_dir / "aiy_birds_v1.onnx"
    if not path.exists():
        pytest.skip(f"Species model not found: {path}")
    return path


@pytest.fixture(scope="session")
def labels_path(models_dir: Path) -> Path:
    """Path to the iNaturalist bird labels file."""
    path = models_dir / "inat_bird_labels.txt"
    if not path.exists():
        pytest.skip(f"Labels file not found: {path}")
    return path


@pytest.fixture(scope="session")
def regional_species_path(models_dir: Path) -> Path:
    """Path to the Chilmark feeder regional species list."""
    path = models_dir / "chilmark_feeder_species.txt"
    if not path.exists():
        pytest.skip(f"Regional species file not found: {path}")
    return path


@pytest.fixture(scope="session")
def regional_species(regional_species_path: Path) -> set:
    """Set of species names from the regional species file."""
    lines = regional_species_path.read_text(encoding="utf-8").splitlines()
    return {line.strip() for line in lines if line.strip()}


# ---------------------------------------------------------------------------
# Test image fixtures  (skip if not present)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def test_bird_image() -> Path:
    """Path to a real classified bird image from the snapshots archive."""
    classified_root = Path("/Users/vives/bird-snapshots/classified")
    if not classified_root.exists():
        pytest.skip(f"Classified snapshots directory not found: {classified_root}")

    # Walk species subdirectories to find the first available .jpg
    for species_dir in sorted(classified_root.iterdir()):
        if not species_dir.is_dir():
            continue
        for image_file in sorted(species_dir.glob("*.jpg")):
            return image_file

    pytest.skip("No classified bird images found in snapshot archive")


@pytest.fixture(scope="session")
def test_bird_image_pil(test_bird_image: Path):
    """PIL Image loaded from the test bird image path."""
    try:
        from PIL import Image
    except ImportError:
        pytest.skip("Pillow (PIL) is not installed")

    return Image.open(test_bird_image).convert("RGB")
