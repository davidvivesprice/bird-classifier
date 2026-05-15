import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "dashboard"))

import classifications_db as cdb
import dashboard.pi_review as pi_review


def _seed_classification(db_path: Path, filename: str, species: str):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(cdb.CREATE_TABLE)
        for idx in cdb.INDEXES:
            conn.execute(idx)
        conn.execute(
            "INSERT INTO classifications "
            "(file, camera, timestamp, source_timestamp, source_date, action, "
            "common_name, confidence, extra_json) "
            "VALUES (?, 'feeder', '2026-05-15T10:00:00', "
            "'2026-05-15T10:00:00', '2026-05-15', 'classified', ?, 0.75, "
            "'{\"model_source\":\"aiy_onnx\"}')",
            (filename, species),
        )
        conn.commit()


def test_classifications_db_uses_demo_db_when_pipeline_test_url_is_set(tmp_path):
    env = {"PIPELINE_TEST_RTSP_URL": "rtsp://localhost:8654/feeder-main"}

    assert cdb.resolve_db_path(env=env, home=tmp_path) == (
        tmp_path / "bird-snapshots" / "logs" / "classifications_demo.db"
    )


def test_pi_review_recent_reads_the_requested_mode_database(tmp_path, monkeypatch):
    live_db = tmp_path / "bird-snapshots" / "logs" / "classifications.db"
    demo_db = tmp_path / "bird-snapshots" / "logs" / "classifications_demo.db"
    reviews_db = tmp_path / "bird-snapshots" / "logs" / "pi_reviews.db"
    _seed_classification(live_db, "live.jpg", "Blue Jay")
    _seed_classification(demo_db, "demo.jpg", "American Goldfinch")

    monkeypatch.setattr(pi_review, "CLASSIFICATIONS_DB_PATH", live_db)
    monkeypatch.setattr(pi_review, "DEMO_CLASSIFICATIONS_DB_PATH", demo_db)
    monkeypatch.setattr(pi_review, "DB_PATH", reviews_db)
    pi_review.init_db()

    live_items = pi_review.recent_classifications(limit=8, mode="live")["items"]
    demo_items = pi_review.recent_classifications(limit=8, mode="demo")["items"]

    assert [item["file"] for item in live_items] == ["live.jpg"]
    assert [item["file"] for item in demo_items] == ["demo.jpg"]
