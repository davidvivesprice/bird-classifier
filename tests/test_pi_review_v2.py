"""pi_review v2 — the review port's airtight contract.

Covers: v1→v2 schema migration, richer verdicts, one-transaction
history+cache writes, client_id idempotency, undo-by-append restoring the
prior state, and the accuracy math excluding skip/trash.
"""
import importlib
import sqlite3

import pytest


@pytest.fixture()
def pr(tmp_path, monkeypatch):
    import dashboard.pi_review as m
    importlib.reload(m)
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "pi_reviews.db")
    monkeypatch.setattr(m, "CLASSIFICATIONS_DB_PATH", tmp_path / "cls.db")
    monkeypatch.setattr(m, "DEMO_CLASSIFICATIONS_DB_PATH", tmp_path / "cls_demo.db")
    monkeypatch.setattr(m, "BIRDNET_DB_PATH", tmp_path / "birdnet.db")
    m.init_db()
    return m


def test_v1_table_migrates_to_v2(tmp_path, monkeypatch):
    import dashboard.pi_review as m
    importlib.reload(m)
    db = tmp_path / "pi_reviews.db"
    monkeypatch.setattr(m, "DB_PATH", db)
    # build a v1-era table with the narrow CHECK + a row
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE pi_reviews ("
            " file TEXT PRIMARY KEY,"
            " verdict TEXT NOT NULL CHECK (verdict IN ('yes','no')),"
            " reviewed_at TEXT NOT NULL,"
            " source_mode TEXT NOT NULL DEFAULT 'live',"
            " model_source TEXT)"
        )
        c.execute("INSERT INTO pi_reviews VALUES ('a.jpg','yes','2026-07-01','live','aiy_onnx')")
    m.init_db()
    with sqlite3.connect(db) as c:
        # old row survived
        assert c.execute("SELECT verdict FROM pi_reviews WHERE file='a.jpg'").fetchone()[0] == "yes"
        # new verdicts now pass the CHECK
        c.execute("INSERT INTO pi_reviews VALUES ('b.jpg','not_a_bird','','2026-07-07','live',NULL)")


def test_verdict_writes_history_and_cache(pr):
    r = pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "Hairy Woodpecker"})
    assert r["ok"] and not r["duplicate"]
    with sqlite3.connect(pr.DB_PATH) as c:
        h = c.execute("SELECT verdict, correct_species FROM pi_review_history").fetchall()
        cache = c.execute("SELECT verdict, correct_species FROM pi_reviews WHERE file='x.jpg'").fetchone()
    assert h == [("no", "Hairy Woodpecker")]
    assert cache == ("no", "Hairy Woodpecker")


def test_client_id_idempotent(pr):
    a = pr.post_verdict("x.jpg", body={"verdict": "yes", "client_id": "c1"})
    b = pr.post_verdict("x.jpg", body={"verdict": "yes", "client_id": "c1"})
    assert not a["duplicate"] and b["duplicate"]
    assert a["history_id"] == b["history_id"]
    with sqlite3.connect(pr.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM pi_review_history").fetchone()[0] == 1


def test_undo_restores_prior_state(pr):
    r1 = pr.post_verdict("x.jpg", body={"verdict": "yes"})
    r2 = pr.post_verdict("x.jpg", body={"verdict": "trash"})
    out = pr.undo_review(r2["history_id"])
    assert out["restored_verdict"] == "yes"
    with sqlite3.connect(pr.DB_PATH) as c:
        assert c.execute("SELECT verdict FROM pi_reviews WHERE file='x.jpg'").fetchone()[0] == "yes"
        # undo appended, deleted nothing
        assert c.execute("SELECT COUNT(*) FROM pi_review_history").fetchone()[0] == 3
    # undoing the FIRST (and only remaining) verdict clears the cache row
    out2 = pr.undo_review(r1["history_id"])
    assert out2["restored_verdict"] is None
    with sqlite3.connect(pr.DB_PATH) as c:
        assert c.execute("SELECT COUNT(*) FROM pi_reviews WHERE file='x.jpg'").fetchone()[0] == 0


def test_invalid_verdict_rejected(pr):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        pr.post_verdict("x.jpg", body={"verdict": "maybe"})


def test_stats_exclude_skip_and_trash_from_accuracy(pr):
    pr.post_verdict("a.jpg", body={"verdict": "yes"})
    pr.post_verdict("b.jpg", body={"verdict": "not_a_bird"})
    pr.post_verdict("c.jpg", body={"verdict": "skip"})
    pr.post_verdict("d.jpg", body={"verdict": "trash"})
    s = pr.review_stats()
    assert s["total_reviewed"] == 4
    assert s["overall_accuracy"] == pytest.approx(0.5)  # 1 yes / (1 yes + 1 not_a_bird)


def test_queue_filters_reviewed_and_enriches(pr, tmp_path):
    with sqlite3.connect(pr.CLASSIFICATIONS_DB_PATH) as c:
        c.execute("CREATE TABLE classifications (id INTEGER PRIMARY KEY, file TEXT,"
                  " source_timestamp TEXT, common_name TEXT, confidence REAL,"
                  " extra_json TEXT, action TEXT)")
        for i, (f, sp) in enumerate([("a.jpg", "Blue Jay"), ("b.jpg", "Gray Catbird"),
                                     ("c.jpg", "Blue Jay")], 1):
            c.execute("INSERT INTO classifications VALUES (?,?,?,?,?,?,?)",
                      (i, f, f"2026-07-07 10:0{i}:00", sp, 0.9,
                       '{"model_source": "aiy_onnx"}', "classified"))
    with sqlite3.connect(pr.BIRDNET_DB_PATH) as c:
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, date TEXT, time TEXT,"
                  " common_name TEXT)")
        c.execute("INSERT INTO notes VALUES (1, '2026-07-07', '10:03:10', 'Blue Jay')")
    pr.post_verdict("b.jpg", body={"verdict": "yes"})
    q = pr.review_queue(limit=10)
    files = [i["file"] for i in q["items"]]
    assert "b.jpg" not in files and set(files) == {"a.jpg", "c.jpg"}
    heard = {i["file"]: i["also_heard"] for i in q["items"]}
    assert heard["c.jpg"] is True      # jay heard 10s after c.jpg's 10:03:00
    assert heard["a.jpg"] is False     # 10:01:00 — outside ±30s
