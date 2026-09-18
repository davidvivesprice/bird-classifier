"""pi_review must-fix 2: server-side species validation, reviewer and
bbox_confirmed on every verdict, unknown_text for off-list names.
"""
import importlib
import sqlite3

import pytest
from fastapi import HTTPException


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


def _cache(pr, file):
    with sqlite3.connect(pr.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return dict(c.execute("SELECT * FROM pi_reviews WHERE file=?", (file,)).fetchone())


def _history(pr, file):
    with sqlite3.connect(pr.DB_PATH) as c:
        c.row_factory = sqlite3.Row
        return [dict(r) for r in c.execute(
            "SELECT * FROM pi_review_history WHERE file=? ORDER BY id", (file,))]


# ── canonical list ──────────────────────────────────────────────────────

def test_canonical_species_built_from_both_label_files(pr):
    names = pr.canonical_species()
    assert "Hairy Woodpecker" in names          # chilmark list
    assert "Blue Jay" in names                  # both
    assert "Limpkin" in names                   # inat only (common name, not "Aramus guarauna (Limpkin)")
    assert "Cassin's Finch" in names
    assert not any("(" in n for n in names)
    assert "background" not in names


def test_canonicalize_is_case_insensitive_and_alias_aware(pr):
    assert pr.canonicalize_species("hairy woodpecker") == "Hairy Woodpecker"
    assert pr.canonicalize_species("  Blue Jay ") == "Blue Jay"
    assert pr.canonicalize_species("Slate-colored Junco") == "Dark-eyed Junco"
    assert pr.canonicalize_species("Purple Space Chicken") is None
    assert pr.canonicalize_species("") is None


# ── validation on POST ──────────────────────────────────────────────────

def test_known_species_stored_canonical(pr):
    r = pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "hairy woodpecker"})
    assert r["correct_species"] == "Hairy Woodpecker"
    row = _cache(pr, "x.jpg")
    assert row["correct_species"] == "Hairy Woodpecker"
    assert row["unknown_text"] is None


def test_unknown_species_is_400(pr):
    with pytest.raises(HTTPException) as ei:
        pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "Purple Space Chicken"})
    assert ei.value.status_code == 400
    assert "Purple Space Chicken" in ei.value.detail
    with sqlite3.connect(pr.DB_PATH) as c:
        assert c.execute("SELECT count(*) FROM pi_review_history").fetchone()[0] == 0


def test_unknown_species_400_suggests_close_match(pr):
    with pytest.raises(HTTPException) as ei:
        pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "Hairy Woodpeker"})
    assert "Hairy Woodpecker" in ei.value.detail


def test_allow_unknown_stores_text_not_species(pr):
    r = pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "Purple Space Chicken",
                                       "allow_unknown": 1})
    assert r["correct_species"] == "" and r["unknown_text"] == "Purple Space Chicken"
    row = _cache(pr, "x.jpg")
    assert row["correct_species"] == "" and row["unknown_text"] == "Purple Space Chicken"
    assert _history(pr, "x.jpg")[0]["unknown_text"] == "Purple Space Chicken"


def test_allow_unknown_query_param(pr):
    r = pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "Purple Space Chicken"},
                        allow_unknown=1)
    assert r["unknown_text"] == "Purple Space Chicken"


def test_allow_unknown_query_accepts_true_and_rejects_0_over_http(pr):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(pr.router)
    client = TestClient(app)
    body = {"verdict": "no", "correct_species": "Nope Bird"}
    ok = client.post("/api/pi-review/a.jpg?allow_unknown=true", json=body)
    assert ok.status_code == 200 and ok.json()["unknown_text"] == "Nope Bird"
    assert client.post("/api/pi-review/b.jpg?allow_unknown=0", json=body).status_code == 400


def test_non_string_correct_species_is_400(pr):
    with pytest.raises(HTTPException) as ei:
        pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": 42})
    assert ei.value.status_code == 400


def test_empty_correct_species_still_allowed_on_no(pr):
    r = pr.post_verdict("x.jpg", body={"verdict": "no"})
    assert r["correct_species"] == "" and r["unknown_text"] is None


def test_species_ignored_unless_verdict_is_no(pr):
    r = pr.post_verdict("x.jpg", body={"verdict": "yes", "correct_species": "Purple Space Chicken"})
    assert r["correct_species"] == ""


# ── reviewer + bbox_confirmed ───────────────────────────────────────────

def test_reviewer_defaults_to_dashboard(pr):
    r = pr.post_verdict("x.jpg", body={"verdict": "yes"})
    assert r["reviewer"] == "dashboard"
    assert _cache(pr, "x.jpg")["reviewer"] == "dashboard"
    assert _history(pr, "x.jpg")[0]["reviewer"] == "dashboard"


def test_reviewer_from_header_via_http(pr):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(pr.router)
    client = TestClient(app)
    resp = client.post("/api/pi-review/x.jpg", json={"verdict": "yes"},
                       headers={"X-Reviewer": "david"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["reviewer"] == "david"
    assert _cache(pr, "x.jpg")["reviewer"] == "david"
    # unknown species over HTTP is a real 400, canonical one a 200
    bad = client.post("/api/pi-review/y.jpg", json={"verdict": "no", "correct_species": "Nope Bird"})
    assert bad.status_code == 400
    ok = client.post("/api/pi-review/y.jpg", json={"verdict": "no", "correct_species": "Blue Jay"})
    assert ok.status_code == 200 and ok.json()["correct_species"] == "Blue Jay"
    q = client.post("/api/pi-review/z.jpg?allow_unknown=1",
                    json={"verdict": "no", "correct_species": "Nope Bird"})
    assert q.status_code == 200 and q.json()["unknown_text"] == "Nope Bird"


def test_bbox_confirmed_accepts_0_1_or_absent(pr):
    pr.post_verdict("a.jpg", body={"verdict": "no", "correct_species": "Blue Jay", "bbox_confirmed": 0})
    pr.post_verdict("b.jpg", body={"verdict": "yes", "bbox_confirmed": "1"})
    pr.post_verdict("c.jpg", body={"verdict": "yes"})
    assert _cache(pr, "a.jpg")["bbox_confirmed"] == 0
    assert _cache(pr, "b.jpg")["bbox_confirmed"] == 1
    assert _cache(pr, "c.jpg")["bbox_confirmed"] is None
    assert _history(pr, "b.jpg")[0]["bbox_confirmed"] == 1


def test_bbox_confirmed_rejects_garbage(pr):
    with pytest.raises(HTTPException) as ei:
        pr.post_verdict("a.jpg", body={"verdict": "yes", "bbox_confirmed": 7})
    assert ei.value.status_code == 400


def test_undo_restores_reviewer_and_bbox(pr):
    pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "Blue Jay", "bbox_confirmed": 0},
                    x_reviewer="david")
    r2 = pr.post_verdict("x.jpg", body={"verdict": "yes"})
    pr.undo_review(r2["history_id"])
    row = _cache(pr, "x.jpg")
    assert (row["verdict"], row["correct_species"], row["reviewer"], row["bbox_confirmed"]) == \
        ("no", "Blue Jay", "david", 0)


def test_clear_and_undo_record_who_did_it(pr):
    pr.post_verdict("x.jpg", body={"verdict": "yes"}, x_reviewer="david")
    pr.clear_verdict("x.jpg", x_reviewer="alice")
    h = _history(pr, "x.jpg")
    assert [(r["verdict"], r["reviewer"]) for r in h] == [("yes", "david"), ("cleared", "alice")]
    r2 = pr.post_verdict("x.jpg", body={"verdict": "no", "correct_species": "Blue Jay"})
    pr.undo_review(r2["history_id"], x_reviewer="bob")
    assert _history(pr, "x.jpg")[-1]["reviewer"] == "bob"


# ── schema migration of an existing v2 DB ───────────────────────────────

def test_existing_v2_db_gains_columns_idempotently(tmp_path, monkeypatch):
    import dashboard.pi_review as m
    importlib.reload(m)
    db = tmp_path / "pi_reviews.db"
    monkeypatch.setattr(m, "DB_PATH", db)
    with sqlite3.connect(db) as c:
        c.execute(m._REVIEWS_SCHEMA_V2)
        c.execute("INSERT INTO pi_reviews VALUES ('a.jpg','no','','2026-07-07','live','aiy_onnx')")
        c.execute("CREATE TABLE pi_review_history (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                  " file TEXT NOT NULL, verdict TEXT NOT NULL, correct_species TEXT NOT NULL DEFAULT '',"
                  " source_mode TEXT NOT NULL DEFAULT 'live', model_source TEXT, client_id TEXT,"
                  " prev_row_id INTEGER, created_at TEXT NOT NULL)")
        c.execute("INSERT INTO pi_review_history (file, verdict, created_at) VALUES ('a.jpg','no','t')")
    m.init_db()
    m.init_db()
    with sqlite3.connect(db) as c:
        for table in ("pi_reviews", "pi_review_history"):
            cols = {r[1]: r[2] for r in c.execute(f"PRAGMA table_info({table})")}
            assert cols["reviewer"] == "TEXT"
            assert cols["bbox_confirmed"] == "INTEGER"
            assert cols["unknown_text"] == "TEXT"
        # historical rows keep NULL reviewer — they were never attributed
        assert c.execute("SELECT reviewer FROM pi_reviews WHERE file='a.jpg'").fetchone()[0] is None
        assert c.execute("SELECT count(*) FROM pi_review_history").fetchone()[0] == 1
