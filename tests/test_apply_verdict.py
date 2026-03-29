"""Tests for _apply_verdict_files() — the file-movement logic behind apply_verdict().

Each test creates a temporary directory structure mimicking bird-snapshots/,
calls _apply_verdict_files() with dir overrides, and asserts file locations.
"""
import sys
from pathlib import Path

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))


from dashboard.api import _apply_verdict_files


def _setup_dirs(tmp_path):
    """Create a minimal classified/annotated/trash/skipped structure."""
    classified = tmp_path / "classified"
    annotated = tmp_path / "annotated"
    trash = tmp_path / "trash"
    skipped = tmp_path / "skipped"
    classified.mkdir()
    annotated.mkdir()
    return classified, annotated, trash, skipped


def _place_file(classified_dir, species, filename, annotated_dir=None):
    """Create a fake image file in classified/<species>/ and optionally annotated/."""
    species_dir = classified_dir / species
    species_dir.mkdir(parents=True, exist_ok=True)
    f = species_dir / filename
    f.write_text("fake image data")
    if annotated_dir:
        ann = annotated_dir / filename
        ann.write_text("annotated data")
    return f


# ── correct verdict: no file movement ──

class TestCorrectVerdict:
    def test_correct_no_movement(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        src = _place_file(classified, "House_Sparrow", "img001.jpg", annotated)

        result = _apply_verdict_files(
            "img001.jpg", "correct", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is False
        assert result["error"] is None
        assert src.exists(), "File should remain in original location"
        assert (annotated / "img001.jpg").exists()


# ── reclassify verdict: no file movement ──

class TestReclassifyVerdict:
    def test_reclassify_no_movement(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        src = _place_file(classified, "House_Sparrow", "img002.jpg", annotated)

        result = _apply_verdict_files(
            "img002.jpg", "reclassify", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is False
        assert result["error"] is None
        assert src.exists()


# ── wrong + correction: file moves to corrected species dir ──

class TestWrongWithCorrection:
    def test_moves_to_corrected_species(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img003.jpg", annotated)

        result = _apply_verdict_files(
            "img003.jpg", "wrong", "Northern Cardinal",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["from_dir"] == "House_Sparrow"
        assert result["to_dir"] == "Northern_Cardinal"
        assert (classified / "Northern_Cardinal" / "img003.jpg").exists()
        assert not (classified / "House_Sparrow" / "img003.jpg").exists()
        # Annotated file should still exist (not trashed)
        assert (annotated / "img003.jpg").exists()

    def test_creates_target_dir(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img004.jpg")

        result = _apply_verdict_files(
            "img004.jpg", "wrong", "Blue Jay",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert (classified / "Blue_Jay").is_dir()
        assert (classified / "Blue_Jay" / "img004.jpg").exists()


# ── wrong + not_a_bird: classified to trash, annotated deleted ──

class TestWrongNotABird:
    def test_moves_to_trash_deletes_annotated(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img005.jpg", annotated)

        result = _apply_verdict_files(
            "img005.jpg", "wrong", "not_a_bird",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "trash"
        assert (trash / "img005.jpg").exists()
        assert not (classified / "House_Sparrow" / "img005.jpg").exists()
        assert not (annotated / "img005.jpg").exists()


# ── trash verdict: classified to trash, annotated deleted ──

class TestTrashVerdict:
    def test_moves_to_trash_deletes_annotated(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img006.jpg", annotated)

        result = _apply_verdict_files(
            "img006.jpg", "trash", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "trash"
        assert (trash / "img006.jpg").exists()
        assert not (classified / "House_Sparrow" / "img006.jpg").exists()
        assert not (annotated / "img006.jpg").exists()

    def test_trash_creates_dir(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img007.jpg")

        # trash dir does not exist yet
        result = _apply_verdict_files(
            "img007.jpg", "trash", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert trash.is_dir()


# ── skip verdict: classified to skipped, annotated stays ──

class TestSkipVerdict:
    def test_moves_to_skipped(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img008.jpg", annotated)

        result = _apply_verdict_files(
            "img008.jpg", "skip", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "skipped"
        assert (skipped / "img008.jpg").exists()
        assert not (classified / "House_Sparrow" / "img008.jpg").exists()
        # Annotated should remain
        assert (annotated / "img008.jpg").exists()

    def test_wrong_without_species_goes_to_skipped(self, tmp_path):
        """wrong verdict with empty correct_species => treated as skip."""
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img009.jpg")

        result = _apply_verdict_files(
            "img009.jpg", "wrong", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "skipped"
        assert (skipped / "img009.jpg").exists()


# ── missing file: returns error ──

class TestMissingFile:
    @pytest.mark.parametrize("verdict,species", [
        ("trash", ""),
        ("wrong", "not_a_bird"),
        ("wrong", "Blue Jay"),
        ("skip", ""),
        ("wrong", ""),
    ])
    def test_missing_file_returns_error(self, tmp_path, verdict, species):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        # No file placed — should get error

        result = _apply_verdict_files(
            "nonexistent.jpg", verdict, species,
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is False
        assert result["error"] is not None
        assert "nonexistent.jpg" in result["error"]


# ── species name sanitization ──

class TestSanitization:
    def test_apostrophe_removed(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img010.jpg")

        result = _apply_verdict_files(
            "img010.jpg", "wrong", "Cooper's Hawk",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "Coopers_Hawk"
        assert (classified / "Coopers_Hawk" / "img010.jpg").exists()

    def test_spaces_replaced(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img011.jpg")

        result = _apply_verdict_files(
            "img011.jpg", "wrong", "Northern Cardinal",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["to_dir"] == "Northern_Cardinal"

    def test_slash_replaced(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_file(classified, "House_Sparrow", "img012.jpg")

        result = _apply_verdict_files(
            "img012.jpg", "wrong", "Yellow-bellied Sapsucker/Red-naped",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["to_dir"] == "Yellow-bellied_Sapsucker-Red-naped"
