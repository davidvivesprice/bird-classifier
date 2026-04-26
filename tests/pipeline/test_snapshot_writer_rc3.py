"""RC3: SnapshotWriter must preserve lock-time vote info, not silently
overwrite it with the write-time authoritative_classify result.

See docs/superpowers/specs/2026-04-25-detection-snapshot-audit-findings.md
for the failure mode this guards against.
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.snapshot_writer import SnapshotWriter


def _make_payload(species: str = "Northern Cardinal",
                  species_conf: float = 0.5,
                  model_source: str = "yard"):
    """Build a SnapshotWriter payload mimicking what process_thread submits."""
    return {
        "camera": "feeder",
        "frame": np.zeros((360, 640, 3), dtype=np.uint8),
        "wall_time_ms": 1000000.0,
        "track_id": 42,
        "species": species,
        "species_confidence": species_conf,
        "model_source": model_source,
        "confidence": 0.85,
        "bbox": [100, 100, 300, 300],
        "frame_count": 5,
        "vote_history": [(species, species_conf)] * 3,
    }


def test_lock_time_values_captured_before_auth_overwrite(monkeypatch):
    """The original p['species']/['species_confidence']/['model_source']
    from process_thread must be readable AFTER the auth call, even if
    auth tries to mutate them.
    """
    # Stub out the file/DB writes — we only care about in-memory state.
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    def fake_insert(entry):
        captured_entry.update(entry)
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification", fake_insert)

    # Stub authoritative_classify to return a DIFFERENT species (the noise case)
    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": "American Goldfinch",
                  "confidence": 0.01,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    payload = _make_payload(species="Northern Cardinal",
                            species_conf=0.5,
                            model_source="yard")
    writer._write_one(payload)

    # The captured entry must record the LOCK-TIME species, not the auth one.
    assert captured_entry["lock_time"]["species"] == "Northern Cardinal", (
        f"lock_time.species was {captured_entry['lock_time']['species']!r}, "
        f"expected 'Northern Cardinal' (the lock-time vote winner)"
    )
    assert captured_entry["lock_time"]["confidence"] == 0.5
    assert captured_entry["lock_time"]["source"] == "yard"


def test_disagreement_flag_true_when_species_differ(monkeypatch):
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": "American Goldfinch", "confidence": 0.01,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species="Northern Cardinal"))

    assert captured_entry["disagreement"] is True
    assert captured_entry["authoritative"]["species"] == "American Goldfinch"
    assert captured_entry["authoritative"]["confidence"] == 0.01


def test_disagreement_flag_false_when_species_match(monkeypatch):
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": "Northern Cardinal", "confidence": 0.85,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species="Northern Cardinal"))

    assert captured_entry["disagreement"] is False
    assert captured_entry["authoritative"]["confidence"] == 0.85


def test_authoritative_none_when_classifier_returns_none(monkeypatch):
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=None)

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species="Northern Cardinal"))

    # authoritative is None → disagreement is False (no second opinion to disagree with)
    assert captured_entry["authoritative"] is None
    assert captured_entry["disagreement"] is False
    # Lock-time values still preserved
    assert captured_entry["lock_time"]["species"] == "Northern Cardinal"


def test_disagreement_flag_ignores_case_and_whitespace(monkeypatch):
    """Yard's 12-class set and AIY's 965-class set were trained independently,
    so 'Northern Cardinal' vs 'northern cardinal ' is a labeling-convention
    mismatch — not a real disagreement."""
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": " northern cardinal", "confidence": 0.7,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species="Northern Cardinal"))

    assert captured_entry["disagreement"] is False
    # Original casing/whitespace preserved in the stored fields
    assert captured_entry["lock_time"]["species"] == "Northern Cardinal"
    assert captured_entry["authoritative"]["species"] == " northern cardinal"


def test_disagreement_flag_when_lock_time_species_is_none(monkeypatch):
    """A track that locked with no winner can have an empty species. The
    flag should still be derivable: any non-empty auth.species disagrees with
    an empty lock-time species, but two empties don't."""
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": "House Finch", "confidence": 0.4,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species=""))

    # Empty lock-time species ≠ "House Finch" → disagreement
    assert captured_entry["disagreement"] is True
    assert captured_entry["lock_time"]["species"] == ""
