"""Tests for the /api/review2/* endpoint family — airtight review API.

Covers:
- POST /api/review2/{filename}: submit verdict with optional client_id,
  idempotent when client_id is reused
- GET /api/review2/history/{filename}: audit trail
- POST /api/review2/undo/{history_id}: reverts a history row
"""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))


_uri = "file:test_api_review2?mode=memory&cache=shared"


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
    # Seed a classification row so apply_verdict/find works against something.
    setup.execute(
        "INSERT OR REPLACE INTO classifications "
        "(file, camera, timestamp, action, common_name) "
        "VALUES ('bird.jpg', 'feeder', '2026-04-24T10:00:00', 'classified', 'House Finch')"
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
    # Stub apply_verdict — tests focus on API + DB, not filesystem.
    monkeypatch.setattr(api, "apply_verdict",
                        lambda filename, verdict, correct_species="": {"moved": False})
    # Cache clear
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


# ── POST /api/review2/{filename} ──────────────────────────────────────


def test_submit_review_returns_history_id(client):
    r = client.post("/api/review2/bird.jpg", json={
        "verdict": "correct",
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["ok"] is True
    assert "history_id" in data
    assert data["duplicate"] is False


def test_submit_review_with_client_id_idempotent(client):
    r1 = client.post("/api/review2/bird.jpg", json={
        "verdict": "correct",
        "client_id": "session-1-card-7",
    })
    r2 = client.post("/api/review2/bird.jpg", json={
        "verdict": "trash",              # different verdict!
        "client_id": "session-1-card-7",  # same client_id
    })
    assert r1.status_code == 200
    assert r2.status_code == 200
    j1 = r1.json()
    j2 = r2.json()
    assert j1["history_id"] == j2["history_id"]
    assert j2["duplicate"] is True


def test_submit_review_without_client_id_still_writes_history(client):
    client.post("/api/review2/bird.jpg", json={"verdict": "correct"})
    r = client.get("/api/review2/history/bird.jpg")
    assert r.status_code == 200
    assert len(r.json()["history"]) == 1


def test_submit_review_wrong_with_correct_species(client):
    r = client.post("/api/review2/bird.jpg", json={
        "verdict": "wrong",
        "correct_species": "Downy Woodpecker",
        "client_id": "c1",
    })
    assert r.status_code == 200
    history = client.get("/api/review2/history/bird.jpg").json()["history"]
    assert history[0]["verdict"] == "wrong"
    assert history[0]["correct_species"] == "Downy Woodpecker"


# ── GET /api/review2/history/{filename} ──────────────────────────────


def test_history_empty_for_unreviewed_file(client):
    r = client.get("/api/review2/history/never-seen.jpg")
    assert r.status_code == 200
    assert r.json()["history"] == []


def test_history_chronological(client):
    client.post("/api/review2/bird.jpg", json={"verdict": "correct", "client_id": "c1"})
    client.post("/api/review2/bird.jpg", json={
        "verdict": "wrong", "correct_species": "Downy Woodpecker", "client_id": "c2",
    })
    client.post("/api/review2/bird.jpg", json={"verdict": "correct", "client_id": "c3"})
    history = client.get("/api/review2/history/bird.jpg").json()["history"]
    assert [h["verdict"] for h in history] == ["correct", "wrong", "correct"]


# ── POST /api/review2/undo/{history_id} ──────────────────────────────


def test_undo_reverts_current_state(client):
    r1 = client.post("/api/review2/bird.jpg", json={"verdict": "correct", "client_id": "c1"})
    r2 = client.post("/api/review2/bird.jpg", json={"verdict": "trash", "client_id": "c2"})
    hid_trash = r2.json()["history_id"]
    u = client.post(f"/api/review2/undo/{hid_trash}", json={"client_id": "u1"})
    assert u.status_code == 200
    assert u.json()["ok"] is True
    history = client.get("/api/review2/history/bird.jpg").json()["history"]
    assert [h["verdict"] for h in history] == ["correct", "trash", "undone"]


def test_undo_unknown_history_id_returns_400(client):
    r = client.post("/api/review2/undo/99999", json={"client_id": "u1"})
    assert r.status_code == 400


def test_undo_idempotent_on_client_id(client):
    r1 = client.post("/api/review2/bird.jpg", json={"verdict": "correct", "client_id": "c1"})
    hid = r1.json()["history_id"]
    u1 = client.post(f"/api/review2/undo/{hid}", json={"client_id": "undo-same"})
    u2 = client.post(f"/api/review2/undo/{hid}", json={"client_id": "undo-same"})
    assert u1.status_code == 200 and u2.status_code == 200
    # Second is a no-op; only one "undone" row in history.
    history = client.get("/api/review2/history/bird.jpg").json()["history"]
    undone_count = sum(1 for h in history if h["verdict"] == "undone")
    assert undone_count == 1
