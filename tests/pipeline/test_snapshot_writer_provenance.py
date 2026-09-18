"""SnapshotWriter must fill the provenance columns on every new row
(post-mortem must-fix 1 + 3): label_source, lock/auth species, image size,
bbox space, crop validity and era — not just bury them in extra_json.
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.snapshot_writer import SnapshotWriter, SNAPSHOT_ERA


def _payload(**over):
    p = {
        "camera": "feeder",
        "frame": np.zeros((360, 640, 3), dtype=np.uint8),
        "wall_time_ms": 1000000.0,
        "pts": 2139.25,
        "track_id": 27,
        "species": "White-breasted Nuthatch",
        "species_confidence": 0.77,
        "model_source": "aiy_onnx",
        "confidence": 0.25,
        "bbox": [100, 100, 300, 300],
        "frame_count": 5,
        "vote_history": [("White-breasted Nuthatch", 0.77)] * 3,
    }
    p.update(over)
    return p


@pytest.fixture()
def captured(monkeypatch):
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)
    entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification", lambda e: entry.update(e))
    return entry


def _auth(species, conf):
    fake = MagicMock()
    fake.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": species, "confidence": conf, "model_source": "aiy_onnx"})())
    return fake


def test_new_row_carries_every_provenance_column(captured):
    writer = SnapshotWriter(classifier=_auth("Black-capped Chickadee", 0.36))
    writer._write_one(_payload())
    e = captured
    assert e["label_source"] == "aiy_onnx"
    assert e["lock_species"] == "White-breasted Nuthatch"
    assert e["auth_species"] == "Black-capped Chickadee"
    assert e["auth_confidence"] == pytest.approx(0.36)
    assert e["disagreement"] is True
    assert e["track_id"] == 27
    assert e["pts"] == pytest.approx(2139.25)
    assert e["model_source"] == "aiy_onnx"
    assert (e["image_w"], e["image_h"]) == (640, 360)
    assert e["bbox_space"] == "image"
    assert e["crop_valid"] == 1
    assert e["era"] == SNAPSHOT_ERA == "hls-pts"


def test_image_size_follows_the_hires_frame(captured, monkeypatch):
    writer = SnapshotWriter(classifier=None)
    monkeypatch.setattr(writer, "_fetch_hls_frame_for_pts",
                        lambda *a, **kw: np.zeros((1080, 1920, 3), dtype=np.uint8))
    writer._write_one(_payload())
    assert (captured["image_w"], captured["image_h"]) == (1920, 1080)
    # bbox was rescaled into the saved image's pixel space
    assert captured["best_detection"]["box"] == [300.0, 300.0, 900.0, 900.0]
    assert captured["crop_valid"] == 1


def test_yard_lock_source_is_quarantined_as_yard(captured):
    writer = SnapshotWriter(classifier=None)
    writer._write_one(_payload(model_source="both_agree"))
    assert captured["label_source"] == "yard"


def test_auth_none_leaves_auth_columns_null(captured):
    writer = SnapshotWriter(classifier=None)
    writer._write_one(_payload())
    assert captured["auth_species"] is None
    assert captured["auth_confidence"] is None
    assert captured["disagreement"] is False


def test_box_outside_image_is_crop_invalid(captured):
    writer = SnapshotWriter(classifier=None)
    writer._write_one(_payload(bbox=[700, 400, 800, 500]))
    assert captured["crop_valid"] == 0


def test_db_failure_removes_raw_and_annotated_jpgs(monkeypatch):
    """No orphan files: when the row cannot be written, both the classified
    JPG and its annotated twin are unlinked and the error propagates."""
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    unlinked = []
    monkeypatch.setattr("pathlib.Path.unlink", lambda self, *a, **kw: unlinked.append(self))
    import classifications_db as cdb

    def boom(_entry):
        raise RuntimeError("db down")
    monkeypatch.setattr(cdb, "insert_classification", boom)

    writer = SnapshotWriter(classifier=None)
    with pytest.raises(RuntimeError, match="db down"):
        writer._write_one(_payload())
    assert {p.parent.name for p in unlinked} == {"White-breasted Nuthatch", "annotated"}
    assert len({p.name for p in unlinked}) == 1
