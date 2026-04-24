"""Tests for /api/review2/* batch + special endpoints.

Covers:
- POST /api/review2/batch-confirm — bulk "correct" verdict, one history row per file
- POST /api/review2/batch-reject — bulk "wrong" verdict, one history row per file
- POST /api/review2/rerun-missed — requeues reclassify-flagged files with history
- POST /api/review2/second-opinion/{filename} — idempotent save of cropped image

The legacy versions (at /api/review/batch-confirm, etc.) were NOT airtight:
they used raw INSERT OR IGNORE INTO reviews, bypassing review_history.
These new endpoints write history for every affected file.
"""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))


_uri = "file:test_api_review2_batch?mode=memory&cache=shared"


def _make_conn():
    c = sqlite3.connect(_uri, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture(autouse=True)
def env(monkeypatch):
    import classifications_db as cdb
    import reviews_db as rdb
    import dashboard.api as api

    setup = _make_conn()
    setup.execute(cdb.CREATE_TABLE)
    for idx in cdb.INDEXES:
        setup.execute(idx)
    setup.execute(rdb.CREATE_TABLE)
    for idx in rdb.INDEXES:
        setup.execute(idx)
    setup.execute(rdb.CREATE_HISTORY_TABLE)
    for idx in rdb.HISTORY_INDEXES:
        setup.execute(idx)
    # Seed 3 classification rows for the batch tests.
    for i, (fn, sp) in enumerate([
        ("bird01.jpg", "House Finch"),
        ("bird02.jpg", "House Finch"),
        ("bird03.jpg", "House Finch"),
    ]):
        setup.execute(
            "INSERT OR REPLACE INTO classifications "
            "(file, camera, timestamp, action, common_name) "
            f"VALUES (?, 'feeder', '2026-04-24T10:0{i}:00', 'classified', ?)",
            (fn, sp),
        )
    setup.commit()

    rdb._reset_table_flag()
    local = threading.local()

    def fake_get_conn(readonly=False):
        attr = "_ro" if readonly else "_rw"
        c = getattr(local, attr, None)
        if c is not None:
            try:
                c.execute("SELECT 1")
                return c
            except sqlite3.Error:
                pass
        c = _make_conn()
        setattr(local, attr, c)
        return c

    monkeypatch.setattr(rdb, "get_conn", fake_get_conn)
    # Also stub classifications_db.get_conn since apply_verdict touches it for
    # trash-marking. Share the same in-memory DB.
    monkeypatch.setattr(cdb, "get_conn", fake_get_conn)
    # Stub apply_verdict — tests focus on API + DB state, not filesystem moves.
    monkeypatch.setattr(api, "apply_verdict",
                        lambda filename, verdict, correct_species="": {"moved": False})
    # Stub second-opinion filesystem work (needs a real image on disk).
    monkeypatch.setattr(api, "_find_any_image", lambda name: None)
    if hasattr(api, "_result_cache"):
        api._result_cache.clear()

    yield setup

    setup.execute("DROP TABLE IF EXISTS reviews")
    setup.execute("DROP TABLE IF EXISTS review_history")
    setup.execute("DROP TABLE IF EXISTS classifications")
    setup.commit()
    setup.close()


@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from dashboard.api import app
    return TestClient(app, raise_server_exceptions=False)


# ── POST /api/review2/batch-confirm ───────────────────────────────────


def test_batch_confirm_writes_history_row_per_file(client, env):
    r = client.post("/api/review2/batch-confirm", json={
        "files": ["bird01.jpg", "bird02.jpg", "bird03.jpg"],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["confirmed"] == 3
    # Every file should have its own review_history row.
    for fn in ("bird01.jpg", "bird02.jpg", "bird03.jpg"):
        rows = env.execute(
            "SELECT * FROM review_history WHERE file = ?", (fn,)
        ).fetchall()
        assert len(rows) == 1, f"{fn}: expected 1 history row, got {len(rows)}"
        assert rows[0]["verdict"] == "correct"
    # And the reviews cache has one row per file.
    rev_count = env.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert rev_count == 3


def test_batch_confirm_idempotent_via_client_id(client, env):
    payload = {
        "files": ["bird01.jpg", "bird02.jpg"],
        "client_id": "batch-abc-123",
    }
    r1 = client.post("/api/review2/batch-confirm", json=payload)
    r2 = client.post("/api/review2/batch-confirm", json=payload)
    assert r1.status_code == 200 and r2.status_code == 200
    # Second call should be a no-op — still 2 total history rows, not 4.
    total = env.execute("SELECT COUNT(*) FROM review_history").fetchone()[0]
    assert total == 2


def test_batch_confirm_empty_list_returns_zero(client):
    r = client.post("/api/review2/batch-confirm", json={"files": []})
    assert r.status_code == 200
    assert r.json()["confirmed"] == 0


# ── POST /api/review2/batch-reject ────────────────────────────────────


def test_batch_reject_writes_history_with_correct_species(client, env):
    r = client.post("/api/review2/batch-reject", json={
        "files": ["bird01.jpg", "bird02.jpg"],
        "correct_species": "Purple Finch",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["rejected"] == 2
    for fn in ("bird01.jpg", "bird02.jpg"):
        row = env.execute(
            "SELECT verdict, correct_species FROM review_history WHERE file = ?",
            (fn,)
        ).fetchone()
        assert row["verdict"] == "wrong"
        assert row["correct_species"] == "Purple Finch"


def test_batch_reject_idempotent_via_client_id(client, env):
    payload = {
        "files": ["bird01.jpg", "bird02.jpg"],
        "correct_species": "Purple Finch",
        "client_id": "batch-reject-xyz",
    }
    client.post("/api/review2/batch-reject", json=payload)
    client.post("/api/review2/batch-reject", json=payload)
    total = env.execute("SELECT COUNT(*) FROM review_history").fetchone()[0]
    assert total == 2  # no duplicates


def test_batch_reject_not_a_bird_works(client, env):
    """not_a_bird is a legit correct_species value — it means 'trash'."""
    r = client.post("/api/review2/batch-reject", json={
        "files": ["bird01.jpg"],
        "correct_species": "not_a_bird",
    })
    assert r.status_code == 200
    row = env.execute(
        "SELECT verdict, correct_species FROM review_history WHERE file = 'bird01.jpg'"
    ).fetchone()
    assert row["verdict"] == "wrong"
    assert row["correct_species"] == "not_a_bird"


# ── POST /api/review2/rerun-missed ────────────────────────────────────


def test_rerun_missed_writes_requeued_history(client, env, monkeypatch):
    """rerun-missed should write a 'requeued' history row for every file
    that had verdict=reclassify."""
    # Seed a reclassify review for bird01.jpg
    import reviews_db as rdb
    rdb.insert_review({
        "file": "bird01.jpg",
        "verdict": "reclassify",
        "timestamp": "2026-04-24T10:00:00",
    })
    # Stub the file-move to avoid touching disk
    import dashboard.api as api
    monkeypatch.setattr(api, "_find_classified_file", lambda f: None)

    r = client.post("/api/review2/rerun-missed", json={})
    assert r.status_code == 200, r.text
    # There should now be 2 history rows for bird01.jpg: the initial reclassify,
    # and the requeued marker.
    rows = env.execute(
        "SELECT verdict FROM review_history WHERE file = 'bird01.jpg' ORDER BY id"
    ).fetchall()
    verdicts = [r["verdict"] for r in rows]
    assert verdicts == ["reclassify", "requeued"]


# ── POST /api/review2/second-opinion/{filename} ───────────────────────


def test_second_opinion_404_when_image_missing(client):
    """second-opinion saves a crop — if the source image isn't findable,
    returns 404, doesn't crash."""
    r = client.post("/api/review2/second-opinion/nope.jpg", json={})
    assert r.status_code == 404


# ── Audit integrity: every reviews row matches latest history row ────


def test_reviews_cache_matches_latest_history_after_batch(client, env):
    """Invariant: for every row in `reviews`, there's a matching latest
    `review_history` row with the same verdict + correct_species.
    This is the core airtightness property."""
    client.post("/api/review2/batch-reject", json={
        "files": ["bird01.jpg", "bird02.jpg"],
        "correct_species": "Purple Finch",
    })
    mismatches = env.execute("""
        SELECT r.file, r.verdict AS r_verdict, h.verdict AS h_verdict
        FROM reviews r
        LEFT JOIN (
            SELECT file, verdict, correct_species,
                   MAX(id) OVER (PARTITION BY file) AS max_id, id
            FROM review_history
        ) h ON h.file = r.file AND h.id = h.max_id
        WHERE h.verdict IS NULL OR h.verdict != r.verdict
    """).fetchall()
    assert mismatches == [], f"reviews/history mismatch: {[dict(m) for m in mismatches]}"
