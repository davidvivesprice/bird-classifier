"""tools/build_flagship_manifest.py must-fix 3: yard-labelled rows never
enter train/val, crop_valid=0 rows are excluded everywhere, --report
prints label_source × split counts.
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
import build_flagship_manifest as bfm


def _img(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xd8\xff\xd9")


@pytest.fixture()
def world(tmp_path, monkeypatch):
    classified = tmp_path / "classified"
    db = tmp_path / "classifications.db"
    negatives = tmp_path / "negatives"
    monkeypatch.setattr(bfm, "CLASSIFIED", classified)
    monkeypatch.setattr(bfm, "DB", db)
    monkeypatch.setattr(bfm, "NEGATIVES", negatives)

    rows = []   # (file, label_dir, label_source, crop_valid, review)
    for i in range(6):
        rows.append((f"2026-06-{10+i:02d}_a{i}.jpg", "House Finch", "aiy_onnx", 1, None))
    for i in range(4):
        rows.append((f"2026-06-{10+i:02d}_y{i}.jpg", "House Finch", "yard", 1, None))
    rows.append(("2026-04-30_old.jpg", "House Finch", "aiy_onnx", 0, None))
    rows.append(("2026-06-20_t.jpg", "House Finch", "aiy_onnx", 1, ("correct", "")))
    rows.append(("2026-06-21_t.jpg", "Blue Jay", "aiy_onnx", 0, ("correct", "")))
    rows.append(("2026-06-22_t.jpg", "Blue Jay", "yard", 1, ("correct", "")))
    rows.append(("2026-06-23_n.jpg", "House Finch", None, None, None))   # unmigrated row

    with sqlite3.connect(db) as c:
        c.execute(cdb.CREATE_TABLE)
        c.execute("CREATE TABLE reviews (file TEXT PRIMARY KEY, verdict TEXT, correct_species TEXT)")
        for f, label, src, cv, rev in rows:
            _img(classified / label / f)
            c.execute(
                "INSERT INTO classifications (file, timestamp, action, common_name, "
                "best_detection_json, label_source, crop_valid) VALUES (?, 't', 'classified', ?, ?, ?, ?)",
                (f, label, json.dumps({"box": [1, 2, 3, 4]}), src, cv),
            )
            if rev:
                c.execute("INSERT INTO reviews VALUES (?, ?, ?)", (f, *rev))
    _img(negatives / "2026-06-01_neg.jpg")
    return {"out": tmp_path / "manifest.csv", "db": db}


def _manifest(path):
    import csv
    with open(path, newline="") as fh:
        return list(csv.DictReader(fh))


def test_yard_rows_never_enter_train_or_val(world, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bfm", "--out", str(world["out"])])
    bfm.main()
    rows = _manifest(world["out"])
    trainval = [r for r in rows if r["split"] in ("train", "val")]
    assert trainval, "expected some train/val rows"
    assert all(r["label_source"] != "yard" for r in trainval)
    assert not any("_y" in Path(r["path"]).name for r in trainval)
    # yard-labelled but human-confirmed rows may still serve as test truth
    assert any(Path(r["path"]).name == "2026-06-22_t.jpg" and r["split"] == "test" for r in rows)


def test_crop_invalid_rows_excluded_from_every_split(world, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bfm", "--out", str(world["out"])])
    bfm.main()
    names = {Path(r["path"]).name for r in _manifest(world["out"])}
    assert "2026-04-30_old.jpg" not in names
    assert "2026-06-21_t.jpg" not in names


def test_unmigrated_rows_are_not_trusted_into_train(world, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bfm", "--out", str(world["out"])])
    bfm.main()
    rows = _manifest(world["out"])
    assert not any(Path(r["path"]).name == "2026-06-23_n.jpg" and r["split"] in ("train", "val")
                   for r in rows)


def test_report_prints_label_source_by_split(world, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["bfm", "--out", str(world["out"]), "--report"])
    bfm.main()
    out = capsys.readouterr().out
    assert "label_source" in out
    assert "aiy_onnx" in out and "yard" in out
    assert "train" in out and "test" in out
    assert "excluded" in out.lower()


def test_manifest_carries_label_source_column(world, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["bfm", "--out", str(world["out"])])
    bfm.main()
    rows = _manifest(world["out"])
    assert {"path", "label", "split", "source", "bbox", "label_source", "crop_valid"} <= set(rows[0].keys())
    train_src = {r["label_source"] for r in rows if r["split"] in ("train", "val") and r["label"] != "not_a_bird"}
    assert train_src == {"aiy_onnx"}


def test_refuses_when_a_train_row_is_yard(world, monkeypatch):
    """Belt and braces: if the pool filter is ever bypassed, the final
    assertion still stops the manifest from being written."""
    monkeypatch.setattr(sys, "argv", ["bfm", "--out", str(world["out"])])
    monkeypatch.setattr(bfm, "TRAIN_LABEL_SOURCES", frozenset({"aiy_onnx", "yard"}))
    with pytest.raises(SystemExit) as ei:
        bfm.main()
    assert ei.value.code != 0
    assert not world["out"].exists()
