"""tools/migrate_provenance.py — backfill provenance columns from extra_json.

Runs against a synthetic DB shaped like the Pi's classifications.db: some
rows carry the full lock_time/authoritative/pts payload, some only
model_source/track_id (pre-RC3), some straddle the 2026-05-12 crop-validity
cutoff.
"""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tools"))

import classifications_db as cdb
import migrate_provenance as mp


def _seed(path: Path, rows):
    with sqlite3.connect(path) as c:
        c.execute(cdb.CREATE_TABLE)
        for f, ts, extra, name in rows:
            c.execute(
                "INSERT INTO classifications (file, camera, timestamp, source_timestamp, "
                "source_date, action, common_name, best_detection_json, extra_json) "
                "VALUES (?, 'feeder', ?, ?, ?, 'classified', ?, ?, ?)",
                (f, ts, ts, ts[:10], name,
                 json.dumps({"box": [10, 10, 100, 100], "confidence": 0.5}),
                 json.dumps(extra) if extra is not None else None),
            )


FULL = {"pipeline_source": "bird_pipeline_v3", "track_id": 27, "model_source": "aiy_onnx",
        "lock_time": {"species": "White-breasted Nuthatch", "confidence": 0.77, "source": "aiy_onnx"},
        "authoritative": {"species": "Black-capped Chickadee", "confidence": 0.36, "source": "aiy_onnx"},
        "disagreement": True, "pts": 2139.27}
EARLY = {"pipeline_source": "bird_pipeline_v3", "track_id": 2, "model_source": "aiy_onnx"}
YARD = {"track_id": 9, "model_source": "yard", "pts": 3.0,
        "lock_time": {"species": "Dark-eyed Junco", "confidence": 0.5, "source": "yard"},
        "authoritative": None, "disagreement": False}


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "classifications-copy.db"
    _seed(path, [
        ("new.jpg", "2026-09-18T11:04:25.462912", FULL, "White-breasted Nuthatch"),
        ("old.jpg", "2026-04-24T05:41:28.925372", EARLY, "Downy Woodpecker"),
        ("edge.jpg", "2026-05-12T00:00:01.553235", FULL, "White-breasted Nuthatch"),
        ("last_old.jpg", "2026-05-11T23:59:58.891560", FULL, "White-breasted Nuthatch"),
        ("yard.jpg", "2026-06-01T08:00:00.000000", YARD, "Dark-eyed Junco"),
        ("noextra.jpg", "2026-06-02T08:00:00.000000", None, "House Finch"),
    ])
    return path


def _rows(path):
    with sqlite3.connect(path) as c:
        c.row_factory = sqlite3.Row
        return {r["file"]: dict(r) for r in c.execute("SELECT * FROM classifications")}


def test_dry_run_changes_nothing(db, capsys):
    before = _rows(db)
    rc = mp.main(["--db", str(db), "--dry-run"])
    assert rc == 0
    assert _rows(db) == before
    out = capsys.readouterr().out
    assert "dry-run" in out.lower()


def test_backfill_fills_columns_and_era(db):
    assert mp.main(["--db", str(db)]) == 0
    r = _rows(db)
    new = r["new.jpg"]
    assert new["label_source"] == "aiy_onnx"
    assert new["lock_species"] == "White-breasted Nuthatch"
    assert new["auth_species"] == "Black-capped Chickadee"
    assert new["auth_confidence"] == pytest.approx(0.36)
    assert new["disagreement"] == 1
    assert new["track_id"] == 27
    assert new["pts"] == pytest.approx(2139.27)
    assert new["model_source"] == "aiy_onnx"
    assert new["bbox_space"] == "image"
    assert (new["crop_valid"], new["era"]) == (1, "hls-pts")

    old = r["old.jpg"]
    assert old["label_source"] == "aiy_onnx"
    assert old["lock_species"] == "Downy Woodpecker"      # no lock_time → common_name
    assert old["auth_species"] is None and old["disagreement"] is None
    assert old["track_id"] == 2 and old["pts"] is None
    assert (old["crop_valid"], old["era"]) == (0, "pre-hls-pts")

    assert (r["edge.jpg"]["crop_valid"], r["edge.jpg"]["era"]) == (1, "hls-pts")
    assert (r["last_old.jpg"]["crop_valid"], r["last_old.jpg"]["era"]) == (0, "pre-hls-pts")

    assert r["yard.jpg"]["label_source"] == "yard"
    # no extra_json at all: era flags still set, label provenance unknown
    ne = r["noextra.jpg"]
    assert ne["label_source"] is None and ne["lock_species"] == "House Finch"
    assert (ne["crop_valid"], ne["era"]) == (1, "hls-pts")


def test_second_run_is_noop(db):
    mp.main(["--db", str(db)])
    after_first = _rows(db)
    rc = mp.main(["--db", str(db)])
    assert rc == 0
    assert _rows(db) == after_first


def test_resumes_after_partial_batch(db):
    """Batch commits: a run that stops mid-way leaves fully-migrated rows
    behind, and the next run only touches the rest."""
    assert mp.main(["--db", str(db), "--batch-size", "2", "--max-batches", "1"]) == 0
    r = _rows(db)
    done = [f for f, row in r.items() if row["era"] is not None]
    assert len(done) == 2
    assert mp.main(["--db", str(db), "--batch-size", "2"]) == 0
    assert all(row["era"] is not None for row in _rows(db).values())


def test_prints_per_label_source_counts_and_acceptance(db, capsys):
    mp.main(["--db", str(db)])
    out = capsys.readouterr().out
    assert "aiy_onnx" in out and "yard" in out
    assert "pre-hls-pts" in out and "hls-pts" in out
    assert "acceptance" in out.lower()


def test_acceptance_query_holds_for_hls_pts_rows(db):
    mp.main(["--db", str(db)])
    with sqlite3.connect(db) as c:
        n = c.execute(
            "SELECT count(*) FROM classifications WHERE action='classified' AND era='hls-pts' "
            "AND label_source IS NOT NULL "
            "AND (track_id IS NULL OR pts IS NULL)"
        ).fetchone()[0]
    assert n == 0


def test_refuses_live_db_path_without_flag(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    _seed(live, [])
    monkeypatch.setattr(mp, "LIVE_DB_PATH", live)
    assert mp.main(["--db", str(live)]) != 0
    assert mp.main(["--db", str(live), "--dry-run"]) == 0


def _snapshot_tree(tmp_path, monkeypatch) -> Path:
    """<root>/<species>/<file>.jpg for two of the seeded rows: a 1080p new.jpg
    and an 8×8 yard.jpg whose seeded box [10,10,100,100] misses the image."""
    from PIL import Image
    root = tmp_path / "classified"
    (root / "White-breasted Nuthatch").mkdir(parents=True)
    Image.new("RGB", (1920, 1080)).save(root / "White-breasted Nuthatch" / "new.jpg")
    (root / "Dark-eyed Junco").mkdir()
    Image.new("RGB", (8, 8)).save(root / "Dark-eyed Junco" / "yard.jpg")
    monkeypatch.setattr(mp, "SNAPSHOT_ROOT", root)
    return root


def test_image_dims_from_jpeg_header(db, tmp_path, monkeypatch):
    _snapshot_tree(tmp_path, monkeypatch)
    assert mp.main(["--db", str(db), "--image-dims"]) == 0
    r = _rows(db)
    assert (r["new.jpg"]["image_w"], r["new.jpg"]["image_h"]) == (1920, 1080)
    assert r["new.jpg"]["crop_valid"] == 1
    assert (r["yard.jpg"]["image_w"], r["yard.jpg"]["image_h"]) == (8, 8)
    assert r["yard.jpg"]["crop_valid"] == 0        # box entirely outside the image
    assert r["old.jpg"]["image_w"] is None   # file absent → left NULL


def test_force_without_image_dims_keeps_dims_and_overflow(db, tmp_path, monkeypatch):
    """A bare --force re-run must not NULL the dims an earlier --image-dims
    pass filled, nor undo the crop_valid=0 those dims justified."""
    _snapshot_tree(tmp_path, monkeypatch)
    assert mp.main(["--db", str(db), "--image-dims"]) == 0
    before = _rows(db)
    assert mp.main(["--db", str(db), "--force"]) == 0
    after = _rows(db)
    assert (after["new.jpg"]["image_w"], after["new.jpg"]["image_h"]) == (1920, 1080)
    assert (after["yard.jpg"]["image_w"], after["yard.jpg"]["crop_valid"]) == (8, 0)
    assert after == before


def test_dry_run_on_legacy_db_without_provenance_columns(tmp_path, capsys):
    """Pre-migration DB: dry-run must not SELECT columns that do not exist."""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE classifications (id INTEGER PRIMARY KEY, file TEXT, "
                  "common_name TEXT, timestamp TEXT, source_timestamp TEXT, "
                  "best_detection_json TEXT, extra_json TEXT)")
        c.execute("INSERT INTO classifications (file, timestamp, extra_json) "
                  "VALUES ('a.jpg', '2026-06-01T00:00:00', ?)", (json.dumps(FULL),))
    assert mp.main(["--db", str(path), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would add columns" in out and "1 rows would write" in out
    with sqlite3.connect(path) as c:
        assert "era" not in {r[1] for r in c.execute("PRAGMA table_info(classifications)")}
