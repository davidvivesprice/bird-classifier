"""Tests for the Dashboard API endpoints using FastAPI TestClient.

Uses monkeypatching to avoid hitting the real SQLite database or filesystem.
"""
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))


# ── Shared in-memory DB for the pending endpoint's direct SQL queries ──

_test_db_uri = "file:test_api_ep?mode=memory&cache=shared"


def _make_in_memory_conn():
    """Return a fresh connection to the shared in-memory test DB."""
    conn = sqlite3.connect(_test_db_uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(autouse=True)
def patch_db_modules(monkeypatch):
    """Patch cdb, rdb, vdb so api.py never touches the real DB."""
    import classifications_db as cdb
    import reviews_db as rdb
    import visits_db as vdb
    import dashboard.api as api

    # Set up an in-memory DB with the required tables
    setup_conn = _make_in_memory_conn()
    setup_conn.execute(cdb.CREATE_TABLE)
    for idx in cdb.INDEXES:
        setup_conn.execute(idx)
    setup_conn.execute(rdb.CREATE_TABLE)
    for idx in rdb.INDEXES:
        setup_conn.execute(idx)
    setup_conn.commit()

    rdb._reset_table_flag()

    # classifications_db stubs
    monkeypatch.setattr(cdb, "init_db", lambda: None)
    monkeypatch.setattr(cdb, "count_total", lambda: 42)
    monkeypatch.setattr(cdb, "count_species", lambda: 5)
    monkeypatch.setattr(cdb, "count_classified", lambda: 30)
    monkeypatch.setattr(cdb, "get_last_timestamp", lambda: "2026-03-28T12:00:00")
    monkeypatch.setattr(cdb, "get_stats", lambda date=None, camera=None: {
        "total": 42,
        "classified": 30,
        "skipped": 10,
        "species_count": 5,
        "last_updated": "2026-03-28T12:00:00",
    })
    monkeypatch.setattr(cdb, "get_species_list", lambda date=None, camera=None: [
        {"species": "Northern Cardinal", "count": 10, "name": "Northern Cardinal"},
        {"species": "Blue Jay", "count": 8, "name": "Blue Jay"},
    ])
    monkeypatch.setattr(cdb, "get_recent", lambda limit=50, camera=None: [])
    monkeypatch.setattr(cdb, "get_species_counts_for_activity", lambda: [
        {"name": "Northern Cardinal", "count": 10},
        {"name": "Blue Jay", "count": 8},
    ])

    # reviews_db stubs
    monkeypatch.setattr(rdb, "count_reviews", lambda: 15)
    monkeypatch.setattr(rdb, "get_pending_classifications",
                        lambda species=None, multibird=False, offset=0, limit=50: [])
    monkeypatch.setattr(rdb, "count_pending", lambda species=None, multibird=False: 0)
    # Patch get_conn to return in-memory DB (for direct SQL in pending endpoint)
    monkeypatch.setattr(rdb, "get_conn", lambda readonly=False: _make_in_memory_conn())

    # visits_db stubs
    monkeypatch.setattr(vdb, "get_visit_summary", lambda date=None: [])

    # Patch BirdNET DB connections
    monkeypatch.setattr(api, "_birdnet_db", lambda: _mock_birdnet_conn())
    monkeypatch.setattr(api, "_get_food_conn", lambda: _mock_birdnet_conn())

    # Clear any cached results from previous tests
    api._result_cache.clear()

    yield

    # Cleanup
    setup_conn.execute("DROP TABLE IF EXISTS reviews")
    setup_conn.execute("DROP TABLE IF EXISTS classifications")
    setup_conn.commit()
    setup_conn.close()


@pytest.fixture()
def client():
    """Create a TestClient for the FastAPI app."""
    from starlette.testclient import TestClient
    from dashboard.api import app
    return TestClient(app, raise_server_exceptions=False)


# ── Health endpoint ──

class TestHealthEndpoint:
    def test_health_returns_200(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200

    def test_health_has_timestamp(self, client):
        data = client.get("/api/health").json()
        assert "timestamp" in data

    def test_health_has_status_ok(self, client):
        data = client.get("/api/health").json()
        assert data["status"] == "ok"


# ── Stats endpoint ──

class TestStatsEndpoint:
    def test_stats_returns_200(self, client):
        r = client.get("/api/stats")
        assert r.status_code == 200

    def test_stats_has_classified(self, client):
        data = client.get("/api/stats").json()
        assert "classified" in data

    def test_stats_has_species_count(self, client):
        data = client.get("/api/stats").json()
        assert "species_count" in data

    def test_stats_has_server_tz_offset(self, client):
        data = client.get("/api/stats").json()
        assert "server_tz_offset" in data

    def test_stats_values_are_integers(self, client):
        data = client.get("/api/stats").json()
        assert isinstance(data["classified"], int)
        assert isinstance(data["species_count"], int)


# ── Review pending endpoint ──

class TestReviewPending:
    def test_pending_returns_200(self, client):
        r = client.get("/api/review/pending")
        assert r.status_code == 200

    def test_pending_has_pending_list(self, client):
        data = client.get("/api/review/pending").json()
        assert "pending" in data

    def test_pending_has_remaining(self, client):
        data = client.get("/api/review/pending").json()
        assert "remaining" in data

    def test_pending_has_total_counts(self, client):
        data = client.get("/api/review/pending").json()
        assert "total_classified" in data
        assert "total_reviewed" in data


# ── Activity species-list endpoint ──

class TestActivitySpeciesList:
    def test_species_list_returns_200(self, client):
        r = client.get("/api/activity/species-list")
        assert r.status_code == 200

    def test_species_list_has_species(self, client):
        data = client.get("/api/activity/species-list").json()
        assert "species" in data
        assert isinstance(data["species"], list)

    def test_species_list_entries_have_name_and_count(self, client):
        data = client.get("/api/activity/species-list").json()
        for item in data["species"]:
            assert "name" in item
            assert "count" in item


# ── Image endpoint (404 for nonexistent) ──

class TestImageEndpoint:
    def test_nonexistent_image_returns_404(self, client):
        r = client.get("/api/image/nonexistent_abc123.jpg")
        assert r.status_code == 404

    def test_nonexistent_raw_image_returns_404(self, client):
        r = client.get("/api/image-raw/nonexistent_abc123.jpg")
        assert r.status_code == 404


# ── Activity heatmap endpoint ──

class TestActivityHeatmap:
    def test_heatmap_returns_200(self, client, monkeypatch):
        import classifications_db as cdb
        monkeypatch.setattr(cdb, "get_conn", lambda readonly=False: _mock_cdb_conn())

        r = client.get("/api/activity/heatmap")
        assert r.status_code == 200

    def test_heatmap_has_expected_keys(self, client, monkeypatch):
        import classifications_db as cdb
        monkeypatch.setattr(cdb, "get_conn", lambda readonly=False: _mock_cdb_conn())

        data = client.get("/api/activity/heatmap").json()
        assert "heatmap" in data
        assert "species" in data
        assert "days" in data


# ── Helpers ──

def _mock_birdnet_conn():
    """Return a mock SQLite-like connection for BirdNET queries."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    cursor.fetchone.return_value = None
    cursor.execute = MagicMock(return_value=cursor)
    conn.cursor.return_value = cursor
    conn.execute = MagicMock(return_value=cursor)
    return conn


def _mock_cdb_conn():
    """Return a mock connection for classifications_db heatmap queries."""
    conn = MagicMock()
    cursor = MagicMock()
    cursor.fetchall.return_value = []
    conn.execute = MagicMock(return_value=cursor)
    return conn
