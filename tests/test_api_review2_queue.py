"""Tests for GET /api/review2/queue — keyset pagination.

The bug this prevents: OFFSET pagination + trash-as-you-go drops items.
With keyset pagination, the cursor is the timestamp of the last item you
saw. Trashing an item on page 1 can't make page 2 skip anything because
the cursor is absolute, not relative.
"""
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))


_uri = "file:test_api_queue?mode=memory&cache=shared"


def _conn():
    c = sqlite3.connect(_uri, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture(autouse=True)
def env(monkeypatch):
    import classifications_db as cdb
    import reviews_db as rdb
    import dashboard.api as api

    s = _conn()
    s.execute(cdb.CREATE_TABLE)
    for idx in cdb.INDEXES:
        s.execute(idx)
    s.execute(rdb.CREATE_TABLE)
    for idx in rdb.INDEXES:
        s.execute(idx)
    s.execute(rdb.CREATE_HISTORY_TABLE)
    for idx in rdb.HISTORY_INDEXES:
        s.execute(idx)

    # Seed 12 classifications with decreasing timestamps — simulates the
    # dashboard's "newest first" pending order. Files bird00..bird11.
    for i in range(12):
        ts = f"2026-04-24T10:{59 - i:02d}:00"
        s.execute(
            "INSERT INTO classifications (file, camera, timestamp, source_timestamp, "
            "  action, common_name) VALUES (?, 'feeder', ?, ?, 'classified', 'House Finch')",
            (f"bird{i:02d}.jpg", ts, ts),
        )
    s.commit()

    rdb._reset_table_flag()
    local = threading.local()

    def fake_get_conn(readonly=False):
        attr = "_ro" if readonly else "_rw"
        c = getattr(local, attr, None)
        if c is not None:
            try:
                c.execute("SELECT 1"); return c
            except sqlite3.Error:
                pass
        c = _conn()
        setattr(local, attr, c)
        return c

    monkeypatch.setattr(rdb, "get_conn", fake_get_conn)
    monkeypatch.setattr(api, "apply_verdict",
                        lambda filename, verdict, correct_species="": {"moved": False})
    if hasattr(api, "_result_cache"):
        api._result_cache.clear()

    yield s
    s.execute("DROP TABLE IF EXISTS reviews")
    s.execute("DROP TABLE IF EXISTS review_history")
    s.execute("DROP TABLE IF EXISTS classifications")
    s.commit()
    s.close()


@pytest.fixture()
def client():
    from starlette.testclient import TestClient
    from dashboard.api import app
    return TestClient(app, raise_server_exceptions=False)


# ── Queue basic behavior ──────────────────────────────────────────────


def test_queue_returns_items_newest_first(client):
    r = client.get("/api/review2/queue?limit=5")
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 5
    # newest first: bird00 has the latest timestamp
    files = [it["file"] for it in data["items"]]
    assert files[0] == "bird00.jpg"
    assert files[-1] == "bird04.jpg"


def test_queue_returns_cursor_for_next_page(client):
    r = client.get("/api/review2/queue?limit=5")
    data = r.json()
    assert "next_cursor" in data
    # Cursor should be a non-empty string (the timestamp of the last item)
    assert data["next_cursor"]


def test_queue_cursor_advances_correctly(client):
    r1 = client.get("/api/review2/queue?limit=5").json()
    cursor = r1["next_cursor"]
    r2 = client.get(f"/api/review2/queue?limit=5&after={cursor}").json()
    files1 = [it["file"] for it in r1["items"]]
    files2 = [it["file"] for it in r2["items"]]
    # No overlap between the two pages
    assert set(files1).isdisjoint(set(files2))
    # r2 continues where r1 left off
    assert files2[0] == "bird05.jpg"


def test_queue_returns_null_cursor_when_exhausted(client):
    # Fewer items than the limit → no next page
    r = client.get("/api/review2/queue?limit=100")
    data = r.json()
    # All 12 items in one shot
    assert len(data["items"]) == 12
    assert data["next_cursor"] is None


# ── Keyset stability under mutation (the whole point) ─────────────────


def test_trashing_between_pages_does_not_skip_items(client):
    """The airtight property: after page 1, if you trash an item from page 1,
    your next-page cursor still points at the right place. With OFFSET you'd
    skip items; with keyset the cursor is absolute so no items are missed."""
    r1 = client.get("/api/review2/queue?limit=5").json()
    files1 = [it["file"] for it in r1["items"]]
    cursor = r1["next_cursor"]

    # Trash one item from page 1 (bird02.jpg, middle of the page)
    trash_resp = client.post("/api/review2/bird02.jpg", json={
        "verdict": "trash", "client_id": "trash-bird02",
    })
    assert trash_resp.status_code == 200

    # Now fetch page 2 with the original cursor
    r2 = client.get(f"/api/review2/queue?limit=5&after={cursor}").json()
    files2 = [it["file"] for it in r2["items"]]

    # Critical invariant: no bird in page 2 was supposed to be on page 1.
    # bird05..bird09 should still show up cleanly.
    assert files2[0] == "bird05.jpg"
    # And bird02 is NOT in page 2 (it got trashed and is gone from pending queries)
    assert "bird02.jpg" not in files2


def test_queue_excludes_reviewed_items(client):
    """Review a bird; it should not appear in the pending queue."""
    client.post("/api/review2/bird00.jpg", json={
        "verdict": "correct", "client_id": "r1",
    })
    r = client.get("/api/review2/queue?limit=50").json()
    files = [it["file"] for it in r["items"]]
    assert "bird00.jpg" not in files
    # Everything else still there
    assert len(files) == 11
