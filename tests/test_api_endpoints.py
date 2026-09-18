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


# ── /api/birdnet-summary flood guard (2026-09-17) ─────────────────────────
# One tunnel client issued 5,646 summary requests in 17 min on 2026-09-15 and
# the dashboard was SIGKILLed twice. Past the per-client budget the endpoint
# must answer from memory (or 429 when nothing is cached) and never touch the DB.

def _summary_guard_reset(api):
    api._birdnet_summary_cache = None
    api._birdnet_summary_mtime = 0
    api._summary_hits.clear()


def test_birdnet_summary_flood_returns_429_without_cache(monkeypatch):
    import dashboard.api as api
    from fastapi.testclient import TestClient
    _summary_guard_reset(api)
    calls = {"n": 0}

    def no_db():
        calls["n"] += 1
        return None
    monkeypatch.setattr(api, "_birdnet_db", no_db)
    client = TestClient(api.app)
    codes = [client.get("/api/birdnet-summary",
                        headers={"cf-connecting-ip": "203.0.113.7"}).status_code
             for _ in range(api._SUMMARY_RATE_N + 10)]
    assert codes[:api._SUMMARY_RATE_N] == [200] * api._SUMMARY_RATE_N
    assert set(codes[api._SUMMARY_RATE_N:]) == {429}
    assert calls["n"] == api._SUMMARY_RATE_N  # the flood never reached the DB


def test_birdnet_summary_flood_serves_cache_when_present(monkeypatch):
    import dashboard.api as api
    from fastapi.testclient import TestClient
    _summary_guard_reset(api)
    api._birdnet_summary_cache = {"total_detections": 1, "cached": True}
    api._birdnet_summary_mtime = 0  # stale on purpose
    calls = {"n": 0}

    def no_db():
        calls["n"] += 1
        return None
    monkeypatch.setattr(api, "_birdnet_db", no_db)
    client = TestClient(api.app)
    for _ in range(api._SUMMARY_RATE_N + 10):
        r = client.get("/api/birdnet-summary", headers={"x-forwarded-for": "198.51.100.9, 10.0.0.1"})
        assert r.status_code == 200
    # every over-budget call answered from memory; other clients are unaffected
    assert calls["n"] == api._SUMMARY_RATE_N
    r = client.get("/api/birdnet-summary", headers={"cf-connecting-ip": "203.0.113.99"})
    assert r.status_code == 200 and calls["n"] == api._SUMMARY_RATE_N + 1
    _summary_guard_reset(api)


def test_alerts_endpoint_reads_failure_log(monkeypatch, tmp_path):
    import dashboard.api as api
    from fastapi.testclient import TestClient
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    iso = lambda dt: dt.isoformat(timespec="seconds")
    log = tmp_path / "unit-failures.log"
    log.write_text(
        f"{iso(now - timedelta(days=30))} UNIT_FAILED bird-audio\n"
        f"{iso(now - timedelta(hours=30))} UNIT_FAILED bird-dashboard\n"
        f"{iso(now - timedelta(hours=2))} ALERT feeder-silent 60000 frames but only 3 detections in the last 4 daytime hours\n"
        "garbage line\n")
    monkeypatch.setattr(api, "_ALERT_LOG", log)
    d = TestClient(api.app).get("/api/alerts").json()
    assert d["count"] == 2 and d["count_24h"] == 1
    assert d["items"][0]["kind"] == "ALERT" and d["items"][0]["name"] == "feeder-silent"
    assert d["items"][0]["message"].startswith("60000 frames")
    assert d["items"][1] == {"at": d["items"][1]["at"], "kind": "UNIT_FAILED", "name": "bird-dashboard", "message": ""}


def test_alerts_endpoint_without_log_is_empty(monkeypatch, tmp_path):
    import dashboard.api as api
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api, "_ALERT_LOG", tmp_path / "missing.log")
    assert TestClient(api.app).get("/api/alerts").json() == {"count": 0, "count_24h": 0, "items": []}


def test_go2rtc_hls_proxy_route_is_gone():
    """/api/hls/{path} proxied a raw path into go2rtc's admin API (SSRF via
    '../api/config' over the public tunnel, 2026-09-18). It must not come back."""
    import dashboard.api as api
    from fastapi.testclient import TestClient
    c = TestClient(api.app)
    for path in ("/api/hls/feeder/index.m3u8", "/api/hls/../api/config", "/api/hls/x/../..%2Fapi%2Fconfig"):
        assert c.get(path).status_code == 404, path
    assert not any(getattr(r, "path", "").startswith("/api/hls/") for r in api.app.routes)
