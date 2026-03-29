"""End-to-end review flow tests.

Tests the full review lifecycle using temp directories:
  - Create a temp classified dir with a fake image
  - Call _apply_verdict_files with different verdicts
  - Verify file moved AND state is correct
"""
import sys
from pathlib import Path

import pytest

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


def _place_image(classified_dir, species, filename, annotated_dir=None, content=b"fake JPEG data"):
    """Create a fake image file in classified/<species>/ and optionally annotated/."""
    species_dir = classified_dir / species
    species_dir.mkdir(parents=True, exist_ok=True)
    img = species_dir / filename
    img.write_bytes(content)
    if annotated_dir:
        ann = annotated_dir / filename
        ann.write_bytes(b"annotated data")
    return img


# ── Correct verdict: file stays in place ──

class TestCorrectFlow:
    def test_correct_leaves_file_unchanged(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        src = _place_image(classified, "Northern_Cardinal", "snap_001.jpg", annotated)

        result = _apply_verdict_files(
            "snap_001.jpg", "correct", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is False
        assert result["error"] is None
        assert src.exists()
        assert (annotated / "snap_001.jpg").exists()

    def test_correct_returns_no_from_to(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "Blue_Jay", "snap_002.jpg")

        result = _apply_verdict_files(
            "snap_002.jpg", "correct", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["from_dir"] is None
        assert result["to_dir"] is None


# ── Wrong + correction: file moves to new species dir ──

class TestWrongWithCorrectionFlow:
    def test_wrong_moves_to_corrected_species(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_010.jpg", annotated)

        result = _apply_verdict_files(
            "snap_010.jpg", "wrong", "Northern Cardinal",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "Northern_Cardinal"
        assert (classified / "Northern_Cardinal" / "snap_010.jpg").exists()
        assert not (classified / "House_Sparrow" / "snap_010.jpg").exists()
        # Annotated file should remain (it is not trashed)
        assert (annotated / "snap_010.jpg").exists()

    def test_wrong_creates_new_species_dir(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_011.jpg")

        result = _apply_verdict_files(
            "snap_011.jpg", "wrong", "American Robin",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert (classified / "American_Robin").is_dir()
        assert (classified / "American_Robin" / "snap_011.jpg").exists()

    def test_wrong_records_from_dir(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_012.jpg")

        result = _apply_verdict_files(
            "snap_012.jpg", "wrong", "Blue Jay",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["from_dir"] == "House_Sparrow"
        assert result["to_dir"] == "Blue_Jay"


# ── Trash verdict: file to trash, annotated deleted ──

class TestTrashFlow:
    def test_trash_moves_to_trash_dir(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_020.jpg", annotated)

        result = _apply_verdict_files(
            "snap_020.jpg", "trash", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "trash"
        assert (trash / "snap_020.jpg").exists()
        assert not (classified / "House_Sparrow" / "snap_020.jpg").exists()

    def test_trash_deletes_annotated_copy(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_021.jpg", annotated)

        _apply_verdict_files(
            "snap_021.jpg", "trash", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert not (annotated / "snap_021.jpg").exists()

    def test_trash_creates_trash_dir_if_missing(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_022.jpg")
        assert not trash.exists()

        _apply_verdict_files(
            "snap_022.jpg", "trash", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert trash.is_dir()
        assert (trash / "snap_022.jpg").exists()

    def test_not_a_bird_goes_to_trash(self, tmp_path):
        """wrong + not_a_bird is treated like trash."""
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_023.jpg", annotated)

        result = _apply_verdict_files(
            "snap_023.jpg", "wrong", "not_a_bird",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "trash"
        assert (trash / "snap_023.jpg").exists()
        assert not (annotated / "snap_023.jpg").exists()


# ── Skip verdict: file to skipped, annotated stays ──

class TestSkipFlow:
    def test_skip_moves_to_skipped_dir(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_030.jpg", annotated)

        result = _apply_verdict_files(
            "snap_030.jpg", "skip", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "skipped"
        assert (skipped / "snap_030.jpg").exists()
        assert not (classified / "House_Sparrow" / "snap_030.jpg").exists()

    def test_skip_preserves_annotated(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_031.jpg", annotated)

        _apply_verdict_files(
            "snap_031.jpg", "skip", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert (annotated / "snap_031.jpg").exists()

    def test_wrong_without_species_treated_as_skip(self, tmp_path):
        """wrong verdict with empty correct_species => skip."""
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "snap_032.jpg")

        result = _apply_verdict_files(
            "snap_032.jpg", "wrong", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is True
        assert result["to_dir"] == "skipped"
        assert (skipped / "snap_032.jpg").exists()


# ── Reclassify verdict: no movement ──

class TestReclassifyFlow:
    def test_reclassify_no_movement(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        src = _place_image(classified, "House_Sparrow", "snap_040.jpg", annotated)

        result = _apply_verdict_files(
            "snap_040.jpg", "reclassify", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is False
        assert result["error"] is None
        assert src.exists()


# ── Error cases ──

class TestReviewErrors:
    def test_missing_file_returns_error(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)

        result = _apply_verdict_files(
            "nonexistent.jpg", "trash", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is False
        assert result["error"] is not None
        assert "nonexistent.jpg" in result["error"]

    def test_missing_file_wrong_verdict_returns_error(self, tmp_path):
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)

        result = _apply_verdict_files(
            "ghost.jpg", "wrong", "Blue Jay",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert result["moved"] is False
        assert result["error"] is not None


# ── Multiple operations in sequence ──

class TestSequentialReview:
    def test_review_multiple_files_sequentially(self, tmp_path):
        """Review 3 files with different verdicts in sequence."""
        classified, annotated, trash, skipped = _setup_dirs(tmp_path)
        _place_image(classified, "House_Sparrow", "file_a.jpg", annotated)
        _place_image(classified, "House_Sparrow", "file_b.jpg", annotated)
        _place_image(classified, "House_Sparrow", "file_c.jpg", annotated)

        # Correct
        r1 = _apply_verdict_files(
            "file_a.jpg", "correct", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )
        # Wrong + correction
        r2 = _apply_verdict_files(
            "file_b.jpg", "wrong", "Northern Cardinal",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )
        # Trash
        r3 = _apply_verdict_files(
            "file_c.jpg", "trash", "",
            classified_dir=classified, annotated_dir=annotated,
            trash_dir=trash, skipped_dir=skipped,
        )

        assert r1["moved"] is False  # correct = no move
        assert r2["moved"] is True
        assert r3["moved"] is True

        assert (classified / "House_Sparrow" / "file_a.jpg").exists()
        assert (classified / "Northern_Cardinal" / "file_b.jpg").exists()
        assert (trash / "file_c.jpg").exists()
        assert not (annotated / "file_c.jpg").exists()
