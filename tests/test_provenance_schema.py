"""Provenance columns on `classifications` (post-mortem must-fix 1 + 3).

label_source / lock_species / auth_* / track_id / pts / model_source /
image_w / image_h / bbox_space / crop_valid / era become real columns,
added by an idempotent ALTER TABLE migration at init and filled by
insert_classification from the snapshot-writer entry shape.
"""
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import classifications_db as cdb

LEGACY_CREATE = """
CREATE TABLE IF NOT EXISTS classifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    file            TEXT    UNIQUE NOT NULL,
    camera          TEXT    NOT NULL DEFAULT 'feeder',
    timestamp       TEXT    NOT NULL,
    source_timestamp TEXT,
    source_date     TEXT,
    action          TEXT    NOT NULL,
    detect_ms       REAL,
    classify_ms     REAL,
    total_ms        REAL,
    detections      INTEGER DEFAULT 0,
    best_detection_json TEXT,
    top_prediction_json TEXT,
    top3_json       TEXT,
    raw_top3_json   TEXT,
    birds_json      TEXT,
    common_name     TEXT,
    scientific_name TEXT,
    raw_score       REAL,
    confidence      REAL,
    range_filter_applied INTEGER DEFAULT 0,
    original_species TEXT,
    filter_reason   TEXT,
    extra_json      TEXT
)
"""

EXPECTED_COLUMNS = {
    "label_source": "TEXT", "lock_species": "TEXT", "auth_species": "TEXT",
    "auth_confidence": "REAL", "disagreement": "INTEGER", "track_id": "INTEGER",
    "pts": "REAL", "model_source": "TEXT", "image_w": "INTEGER", "image_h": "INTEGER",
    "bbox_space": "TEXT", "crop_valid": "INTEGER", "era": "TEXT",
}


def _columns(conn):
    return {r[1]: r[2] for r in conn.execute("PRAGMA table_info(classifications)")}


@pytest.fixture()
def file_db(tmp_path, monkeypatch):
    """Point classifications_db at a fresh on-disk DB (init_db needs a path)."""
    path = tmp_path / "classifications.db"
    monkeypatch.setattr(cdb, "DB_PATH", path)
    monkeypatch.setattr(cdb, "_local", threading.local())
    monkeypatch.setattr(cdb, "_schema_ready", False)
    return path


def _writer_entry(**over):
    entry = {
        "file": "feeder_2026-09-18_11-04-25_27.jpg",
        "camera": "feeder",
        "timestamp": "2026-09-18T11:04:26",
        "source_timestamp": "2026-09-18T11:04:25.462912",
        "action": "classified",
        "detections": 1,
        "best_detection": {"box": [1165.2, 511.6, 1276.9, 660.7], "confidence": 0.25},
        "top_prediction": {"common_name": "White-breasted Nuthatch",
                           "scientific_name": None, "raw_score": 77},
        "top3": [],
        "birds": [],
        "pipeline_source": "bird_pipeline_v3",
        "track_id": 27,
        "model_source": "aiy_onnx",
        "lock_time": {"species": "White-breasted Nuthatch", "confidence": 0.77,
                      "source": "aiy_onnx"},
        "authoritative": {"species": "Black-capped Chickadee", "confidence": 0.36,
                          "source": "aiy_onnx"},
        "disagreement": True,
        "pts": 2139.2667,
        "label_source": "aiy_onnx",
        "lock_species": "White-breasted Nuthatch",
        "auth_species": "Black-capped Chickadee",
        "auth_confidence": 0.36,
        "image_w": 1920,
        "image_h": 1080,
        "bbox_space": "image",
        "crop_valid": 1,
        "era": "hls-pts",
    }
    entry.update(over)
    return entry


class TestSchemaMigration:
    def test_legacy_db_gains_provenance_columns(self, file_db):
        with sqlite3.connect(file_db) as c:
            c.execute(LEGACY_CREATE)
            c.execute("INSERT INTO classifications (file, timestamp, action) VALUES ('a.jpg', 't', 'classified')")
        cdb.init_db()
        with sqlite3.connect(file_db) as c:
            cols = _columns(c)
            for name, typ in EXPECTED_COLUMNS.items():
                assert cols.get(name) == typ, f"{name} missing or wrong type: {cols.get(name)}"
            # legacy row survives with NULL provenance
            assert c.execute("SELECT label_source, era FROM classifications").fetchone() == (None, None)

    def test_migration_is_idempotent(self, file_db):
        cdb.init_db()
        first = None
        with sqlite3.connect(file_db) as c:
            first = _columns(c)
        cdb._schema_ready = False
        cdb.init_db()
        cdb._schema_ready = False
        cdb.init_db()
        with sqlite3.connect(file_db) as c:
            assert _columns(c) == first
            assert len([k for k in first if k in EXPECTED_COLUMNS]) == len(EXPECTED_COLUMNS)

    def test_fresh_create_table_has_columns(self, file_db):
        cdb.init_db()
        with sqlite3.connect(file_db) as c:
            assert EXPECTED_COLUMNS.keys() <= _columns(c).keys()


class TestInsertFillsColumns:
    def test_snapshot_writer_entry_fills_every_column(self, file_db):
        cdb.insert_classification(_writer_entry())
        with sqlite3.connect(file_db) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM classifications").fetchone()
        assert r["label_source"] == "aiy_onnx"
        assert r["lock_species"] == "White-breasted Nuthatch"
        assert r["auth_species"] == "Black-capped Chickadee"
        assert r["auth_confidence"] == pytest.approx(0.36)
        assert r["disagreement"] == 1
        assert r["track_id"] == 27
        assert r["pts"] == pytest.approx(2139.2667)
        assert r["model_source"] == "aiy_onnx"
        assert (r["image_w"], r["image_h"]) == (1920, 1080)
        assert r["bbox_space"] == "image"
        assert r["crop_valid"] == 1
        assert r["era"] == "hls-pts"
        # json_extract consumers (pi_review queue/recent) still see these in extra_json
        extra = json.loads(r["extra_json"])
        assert extra["model_source"] == "aiy_onnx"
        assert extra["track_id"] == 27
        assert extra["pts"] == pytest.approx(2139.2667)
        assert extra["lock_time"]["species"] == "White-breasted Nuthatch"
        # promoted-only keys do not get duplicated into extra_json
        for k in ("label_source", "lock_species", "auth_species", "image_w", "crop_valid", "era"):
            assert k not in extra

    def test_acceptance_query_is_zero_for_new_rows(self, file_db):
        cdb.insert_classification(_writer_entry())
        cdb.insert_classification(_writer_entry(file="b.jpg", track_id=3, pts=1.5))
        with sqlite3.connect(file_db) as c:
            n = c.execute(
                "SELECT count(*) FROM classifications WHERE action='classified' "
                "AND (label_source IS NULL OR track_id IS NULL OR pts IS NULL)"
            ).fetchone()[0]
        assert n == 0

    def test_legacy_entry_derives_from_nested_fields(self, file_db):
        """Callers that only supply lock_time/authoritative/model_source (the
        pre-change entry shape) still get the derived columns."""
        e = _writer_entry()
        for k in ("label_source", "lock_species", "auth_species", "auth_confidence",
                  "image_w", "image_h", "bbox_space", "crop_valid", "era"):
            e.pop(k)
        cdb.insert_classification(e)
        with sqlite3.connect(file_db) as c:
            c.row_factory = sqlite3.Row
            r = c.execute("SELECT * FROM classifications").fetchone()
        assert r["label_source"] == "aiy_onnx"
        assert r["lock_species"] == "White-breasted Nuthatch"
        assert r["auth_species"] == "Black-capped Chickadee"
        assert r["auth_confidence"] == pytest.approx(0.36)
        assert r["disagreement"] == 1
        assert r["image_w"] is None and r["era"] is None

    def test_row_to_entry_exposes_provenance(self, file_db):
        cdb.insert_classification(_writer_entry())
        e = cdb.get_entry_by_file("feeder_2026-09-18_11-04-25_27.jpg")
        assert e["label_source"] == "aiy_onnx"
        assert e["crop_valid"] == 1
        assert e["era"] == "hls-pts"
        assert e["image_w"] == 1920


class TestLabelSource:
    @pytest.mark.parametrize("src,expected", [
        ("aiy_onnx", "aiy_onnx"),
        ("aiy", "aiy"),
        ("yard", "yard"),
        ("yard_coral", "yard"),
        ("both_agree", "yard"),
        ("resnet50_hailo", "resnet50_hailo"),
        ("", None),
        (None, None),
    ])
    def test_label_source_for(self, src, expected):
        assert cdb.label_source_for(src) == expected

    def test_derive_prefers_lock_time_source_over_model_source(self):
        p = cdb.derive_provenance({"model_source": "aiy_onnx",
                                   "lock_time": {"species": "Blue Jay", "source": "yard"}},
                                  common_name="Blue Jay")
        assert p["label_source"] == "yard"
        assert p["lock_species"] == "Blue Jay"

    def test_derive_falls_back_to_common_name_without_lock_time(self):
        p = cdb.derive_provenance({"model_source": "aiy_onnx", "track_id": 2},
                                  common_name="Downy Woodpecker")
        assert p["label_source"] == "aiy_onnx"
        assert p["lock_species"] == "Downy Woodpecker"
        assert p["auth_species"] is None
        assert p["disagreement"] is None
        assert p["track_id"] == 2 and p["pts"] is None
