"""unifi_events × pipeline_tracks: coverage, misses, phantoms, lead/lag."""
import sqlite3
import time

import pytest

from pipeline.unifi_events import SCHEMA_UNIFI_EVENTS, compare_with_tracks, earliest_event_ms

# Only the pipeline_tracks columns the compare touches (full DDL lives in
# pipeline/event_store.py, which drags in norfair).
TRACKS_DDL = """CREATE TABLE IF NOT EXISTS pipeline_tracks (
    track_id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera TEXT NOT NULL,
    species TEXT,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    peak_confidence REAL,
    num_frames INTEGER
)"""

HOUR = 3_600_000
T0 = 1_789_740_000_000  # exact hour boundary (T0 % HOUR == 0)
SEC = 1000


def make_db(uri=":memory:"):
    conn = sqlite3.connect(uri, uri=uri.startswith("file:"), check_same_thread=False)
    conn.execute(SCHEMA_UNIFI_EVENTS)
    conn.execute(TRACKS_DDL)
    conn.commit()
    return conn


def add_event(conn, eid, start, end, camera="feeder", etype="motion"):
    conn.execute(
        "INSERT INTO unifi_events (id, camera, type, start_ms, end_ms, smart_types, raw_json, updated_ms) "
        "VALUES (?, ?, ?, ?, ?, NULL, '{}', ?)",
        (eid, camera, etype, start, end, start))
    conn.commit()


def add_track(conn, start, end, camera="feeder", species="Blue Jay"):
    cur = conn.execute(
        "INSERT INTO pipeline_tracks (camera, species, start_time, end_time, peak_confidence, num_frames) "
        "VALUES (?, ?, ?, ?, 0.9, 10)", (camera, species, start, end))
    conn.commit()
    return cur.lastrowid


def compare(conn, **kw):
    kw.setdefault("camera", "feeder")
    kw.setdefault("since_ms", T0)
    kw.setdefault("until_ms", T0 + 24 * HOUR)
    return compare_with_tracks(conn, **kw)


def test_empty_window():
    r = compare(make_db())
    assert r["motion_events"] == 0
    assert r["events_with_track"] == 0
    assert r["coverage"] is None
    assert r["events_without_track"] == {"count": 0, "items": []}
    assert r["tracks_total"] == 0
    assert r["tracks_without_motion"] == {"count": 0, "items": []}
    assert r["lead_lag_ms"] == {"p50": None, "p90": None, "n": 0}
    assert r["hours"] == []


def test_covered_event_and_lag():
    conn = make_db()
    add_event(conn, "e1", T0 + 10 * SEC, T0 + 17 * SEC)
    add_track(conn, T0 + 11 * SEC, T0 + 20 * SEC)
    r = compare(conn)
    assert r["motion_events"] == 1
    assert r["events_with_track"] == 1
    assert r["coverage"] == 1.0
    assert r["events_without_track"]["count"] == 0
    assert r["tracks_total"] == 1
    assert r["tracks_without_motion"]["count"] == 0
    assert r["lead_lag_ms"] == {"p50": 1000, "p90": 1000, "n": 1}


def test_miss_is_listed_with_times():
    conn = make_db()
    add_event(conn, "e1", T0 + 10 * SEC, T0 + 17 * SEC)
    r = compare(conn)
    assert r["events_with_track"] == 0
    assert r["coverage"] == 0.0
    miss = r["events_without_track"]
    assert miss["count"] == 1
    item = miss["items"][0]
    assert item["id"] == "e1"
    assert item["start_ms"] == T0 + 10 * SEC
    assert item["end_ms"] == T0 + 17 * SEC
    assert item["duration_ms"] == 7000
    assert isinstance(item["start"], str) and item["start"][:2] == "20"


def test_phantom_track_is_listed():
    conn = make_db()
    tid = add_track(conn, T0 + 30 * SEC, T0 + 40 * SEC, species="Tufted Titmouse")
    r = compare(conn)
    ph = r["tracks_without_motion"]
    assert ph["count"] == 1
    assert ph["items"][0]["track_id"] == tid
    assert ph["items"][0]["species"] == "Tufted Titmouse"
    assert ph["items"][0]["start_ms"] == T0 + 30 * SEC
    assert ph["items"][0]["duration_ms"] == 10_000


def test_tolerance_lets_an_early_track_cover_and_reports_negative_lag():
    conn = make_db()
    add_event(conn, "e1", T0 + 10 * SEC, T0 + 17 * SEC)
    add_track(conn, T0 + 8 * SEC, T0 + 9 * SEC)  # ends 1 s before Protect fired
    r = compare(conn, tolerance_ms=3000)
    assert r["events_with_track"] == 1
    assert r["lead_lag_ms"]["p50"] == -2000
    assert r["tracks_without_motion"]["count"] == 0

    r = compare(conn, tolerance_ms=500)
    assert r["events_with_track"] == 0
    assert r["tracks_without_motion"]["count"] == 1


def test_first_overlapping_track_defines_lag():
    conn = make_db()
    add_event(conn, "e1", T0 + 10 * SEC, T0 + 30 * SEC)
    add_track(conn, T0 + 15 * SEC, T0 + 18 * SEC)
    add_track(conn, T0 + 12 * SEC, T0 + 14 * SEC)
    add_track(conn, T0 + 25 * SEC, T0 + 29 * SEC)
    r = compare(conn)
    assert r["events_with_track"] == 1
    assert r["lead_lag_ms"]["p50"] == 2000
    assert r["tracks_without_motion"]["count"] == 0


def test_open_event_without_end_uses_default_duration():
    conn = make_db()
    add_event(conn, "e1", T0 + 10 * SEC, None)
    add_track(conn, T0 + 18 * SEC, T0 + 25 * SEC)  # 8 s after start, inside the 10 s default
    r = compare(conn)
    assert r["events_with_track"] == 1
    add_track(conn, T0 + 60 * SEC, T0 + 65 * SEC)  # well past it
    r = compare(conn)
    assert r["tracks_without_motion"]["count"] == 1


def test_window_and_camera_filters():
    conn = make_db()
    add_event(conn, "in", T0 + 10 * SEC, T0 + 12 * SEC)
    add_event(conn, "before", T0 - 10 * SEC, T0 - 5 * SEC)
    add_event(conn, "ground", T0 + 20 * SEC, T0 + 22 * SEC, camera="ground")
    add_event(conn, "smart", T0 + 40 * SEC, T0 + 42 * SEC, etype="smartDetectZone")
    add_track(conn, T0 - 30 * SEC, T0 - 25 * SEC)             # before window
    add_track(conn, T0 + 20 * SEC, T0 + 22 * SEC, camera="ground")
    r = compare(conn)
    assert r["motion_events"] == 1
    assert r["tracks_total"] == 0
    assert r["by_type"] == {"motion": 1, "smartDetectZone": 1}


def test_track_straddling_window_start_still_counts():
    conn = make_db()
    add_event(conn, "e1", T0 + 1 * SEC, T0 + 8 * SEC)
    add_track(conn, T0 - 2 * SEC, T0 + 5 * SEC)
    r = compare(conn)
    assert r["tracks_total"] == 1
    assert r["events_with_track"] == 1


def test_event_straddling_window_start_still_counts_and_covers():
    # Same edge from the other side: the motion began before since_ms and is
    # still running, our track for it is inside the window. Selecting events by
    # start alone dropped the event and reported the track as a phantom.
    conn = make_db()
    add_event(conn, "closed", T0 - 5 * SEC, T0 + 5 * SEC)
    add_event(conn, "open", T0 - 8 * SEC, None)          # 10 s default → ends T0 + 2 s
    add_event(conn, "ended-before", T0 - 20 * SEC, T0 - 15 * SEC)
    add_track(conn, T0 - 4 * SEC, T0 + 3 * SEC)
    r = compare(conn)
    assert r["motion_events"] == 2
    assert r["events_with_track"] == 2
    assert r["tracks_total"] == 1
    assert r["tracks_without_motion"]["count"] == 0
    assert r["events_without_track"]["count"] == 0


def test_earliest_event_ms_is_per_camera_and_none_when_empty():
    conn = make_db()
    assert earliest_event_ms(conn, "feeder") is None
    add_event(conn, "g", T0 - HOUR, T0 - HOUR + SEC, camera="ground")
    add_event(conn, "b", T0 + 20 * SEC, T0 + 25 * SEC)
    add_event(conn, "a", T0 + 10 * SEC, T0 + 15 * SEC)
    assert earliest_event_ms(conn, "feeder") == T0 + 10 * SEC
    assert earliest_event_ms(conn, "ground") == T0 - HOUR
    bare = sqlite3.connect(":memory:")
    bare.execute(TRACKS_DDL)
    with pytest.raises(sqlite3.OperationalError):
        earliest_event_ms(bare, "feeder")


def test_hourly_buckets():
    conn = make_db()
    add_event(conn, "a", T0 + 10 * SEC, T0 + 15 * SEC)
    add_event(conn, "b", T0 + 20 * SEC, T0 + 25 * SEC)
    add_event(conn, "c", T0 + HOUR + 5 * SEC, T0 + HOUR + 9 * SEC)
    add_track(conn, T0 + 11 * SEC, T0 + 14 * SEC)          # covers a
    add_track(conn, T0 + HOUR + 30 * SEC, T0 + HOUR + 33 * SEC)  # phantom in hour 2
    r = compare(conn)
    assert [h["hour_ms"] for h in r["hours"]] == [T0, T0 + HOUR]
    h0, h1 = r["hours"]
    assert (h0["motion"], h0["covered"], h0["tracks"], h0["phantoms"]) == (2, 1, 1, 0)
    assert (h1["motion"], h1["covered"], h1["tracks"], h1["phantoms"]) == (1, 0, 1, 1)
    assert h0["hour"].endswith(":00")


def test_percentiles_nearest_rank():
    conn = make_db()
    for i in range(10):
        s = T0 + i * 60 * SEC
        add_event(conn, f"e{i}", s, s + 20 * SEC)
        add_track(conn, s + i * SEC, s + 15 * SEC)  # lag i seconds
    r = compare(conn)
    assert r["lead_lag_ms"]["n"] == 10
    assert r["lead_lag_ms"]["p50"] == 4000
    assert r["lead_lag_ms"]["p90"] == 8000


def test_miss_list_is_capped_and_newest_first():
    conn = make_db()
    for i in range(60):
        s = T0 + i * 60 * SEC
        add_event(conn, f"e{i:02d}", s, s + 5 * SEC)
    r = compare(conn, max_items=50)
    assert r["events_without_track"]["count"] == 60
    items = r["events_without_track"]["items"]
    assert len(items) == 50
    assert items[0]["id"] == "e59"


def test_missing_table_raises_operational_error():
    conn = sqlite3.connect(":memory:")
    conn.execute(TRACKS_DDL)
    with pytest.raises(sqlite3.OperationalError):
        compare(conn)


# ── GET /api/unifi-compare ──────────────────────────────────────────────────

_URI = "file:unifi_compare_test?mode=memory&cache=shared"


@pytest.fixture()
def api_client(monkeypatch):
    pytest.importorskip("dashboard.api")
    import dashboard.api as api
    from starlette.testclient import TestClient

    keeper = make_db(_URI)
    monkeypatch.setattr(api, "_unifi_compare_conn",
                        lambda: sqlite3.connect(_URI, uri=True, check_same_thread=False))
    monkeypatch.setattr(api, "_unifi_oracle_health", lambda: None)
    yield TestClient(api.app, raise_server_exceptions=False), keeper, api
    keeper.close()


def test_endpoint_returns_compare_payload(api_client):
    client, conn, _ = api_client
    now = int(time.time() * 1000)
    add_event(conn, "e1", now - 120 * SEC, now - 110 * SEC)
    add_track(conn, now - 119 * SEC, now - 100 * SEC)
    r = client.get("/api/unifi-compare?hours=24")
    assert r.status_code == 200
    d = r.json()
    assert d["available"] is True
    assert d["motion_events"] == 1
    assert d["events_with_track"] == 1
    assert d["lead_lag_ms"]["p50"] == 1000
    assert d["window"]["hours"] == 24
    assert d["oracle"]["status"] == "unknown"


def test_endpoint_clips_window_to_oracle_start(api_client):
    client, conn, api = api_client
    now = int(time.time() * 1000)
    add_track(conn, now - 3 * HOUR, now - 3 * HOUR + 5 * SEC)   # before the oracle existed
    add_track(conn, now - 60 * SEC, now - 50 * SEC)             # phantom inside coverage
    api_started = now - 2 * HOUR
    api._unifi_oracle_health = lambda: {"status": "connected", "started_ms": api_started,
                                        "updated_ms": now - 5 * SEC, "reconnects": 0}
    d = client.get("/api/unifi-compare?hours=24").json()
    assert d["tracks_total"] == 1
    assert d["window"]["since_ms"] == api_started
    assert d["window"]["clipped_to_oracle"] is True
    assert d["oracle"]["status"] == "connected"
    assert d["oracle"]["stale"] is False


def test_endpoint_keeps_pre_restart_events_in_window(api_client):
    # The service restarted 5 min ago but has 2 h of events on disk: coverage
    # starts at the earliest stored event, not at the new process start, so
    # the window must not collapse to "since HH:MM" and drop the older events.
    client, conn, api = api_client
    now = int(time.time() * 1000)
    first_event = now - 2 * HOUR
    add_track(conn, now - 3 * HOUR, now - 3 * HOUR + 5 * SEC)   # before any oracle coverage
    add_event(conn, "old", first_event, first_event + 5 * SEC)
    add_track(conn, first_event + SEC, first_event + 4 * SEC)   # covers "old"
    add_event(conn, "new", now - 60 * SEC, now - 55 * SEC)
    api._unifi_oracle_health = lambda: {"status": "connected", "started_ms": now - 5 * 60 * SEC,
                                        "updated_ms": now - 5 * SEC, "reconnects": 1}
    d = client.get("/api/unifi-compare?hours=24").json()
    assert d["window"]["since_ms"] == first_event
    assert d["window"]["clipped_to_oracle"] is True
    assert d["motion_events"] == 2
    assert d["events_with_track"] == 1
    assert d["tracks_total"] == 1                # the 3 h-old track is outside coverage
    assert d["tracks_without_motion"]["count"] == 0


def test_endpoint_does_not_clip_when_coverage_predates_window(api_client):
    client, conn, api = api_client
    now = int(time.time() * 1000)
    add_event(conn, "day-old", now - 30 * HOUR, now - 30 * HOUR + 5 * SEC)
    api._unifi_oracle_health = lambda: {"status": "connected", "started_ms": now - 60 * SEC,
                                        "updated_ms": now - 5 * SEC, "reconnects": 3}
    d = client.get("/api/unifi-compare?hours=24").json()
    assert d["window"]["clipped_to_oracle"] is False
    assert abs(d["window"]["since_ms"] - (now - 24 * HOUR)) < 5 * SEC
    assert d["motion_events"] == 0               # outside the 24 h window


def test_endpoint_clips_to_earliest_event_when_health_file_is_missing(api_client):
    client, conn, _ = api_client                 # fixture health → None
    now = int(time.time() * 1000)
    first_event = now - HOUR
    add_track(conn, now - 2 * HOUR, now - 2 * HOUR + 5 * SEC)
    add_event(conn, "e", first_event, first_event + 5 * SEC)
    d = client.get("/api/unifi-compare?hours=24").json()
    assert d["window"]["since_ms"] == first_event
    assert d["window"]["clipped_to_oracle"] is True
    assert d["tracks_total"] == 0
    assert d["oracle"]["status"] == "unknown"


def test_endpoint_without_table_is_unavailable(api_client, monkeypatch):
    client, _, api = api_client
    bare = sqlite3.connect(":memory:", check_same_thread=False)
    bare.execute(TRACKS_DDL)
    monkeypatch.setattr(api, "_unifi_compare_conn", lambda: bare)
    d = client.get("/api/unifi-compare").json()
    assert d["available"] is False
    assert "unifi_events" in d["reason"]


def test_endpoint_without_db_is_unavailable(api_client, monkeypatch):
    client, _, api = api_client
    monkeypatch.setattr(api, "_unifi_compare_conn", lambda: None)
    d = client.get("/api/unifi-compare").json()
    assert d["available"] is False


def test_endpoint_rejects_bad_hours(api_client):
    client, _, _ = api_client
    assert client.get("/api/unifi-compare?hours=0").status_code == 422
