"""Tests for EventStore."""
import json
import sqlite3
import time
import pytest


def test_schema_is_created(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "pipeline_events" in tables
    assert "pipeline_tracks" in tables
    store.shutdown()


def test_write_event_flushes_to_db(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    store.write_event(
        camera="feeder",
        frame_time_ms=1712700000000,
        track_id=42,
        species="Black-capped Chickadee",
        confidence=0.82,
        model_source="yard",
        bbox=[100, 200, 300, 400],
        is_new=True,
    )
    store.flush()  # force immediate write
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT * FROM pipeline_events").fetchone()
    assert row is not None
    store.shutdown()


def test_query_events_by_time_range(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    for i in range(5):
        store.write_event(
            camera="feeder",
            frame_time_ms=1000 + i * 100,
            track_id=1,
            species="Test",
            confidence=0.9,
            model_source="yard",
            bbox=[0, 0, 10, 10],
            is_new=(i == 0),
        )
    store.flush()
    results = store.query_events(camera="feeder", start_ms=1100, end_ms=1300)
    assert len(results) == 3
    store.shutdown()


def test_write_track_summary(tmp_path):
    from pipeline.event_store import EventStore
    from pipeline.tracker import Track
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    t = Track(
        track_id=42,
        created_at_ms=1000,
        last_updated_ms=5000,
        bbox=[100, 100, 200, 200],
        confidence=0.85,
        species="Downy Woodpecker",
        model_source="yard",
    )
    # Populate motion history for motion_pct calculation
    for i in range(10):
        t.motion_history.append((100 + i, 100))
    store.write_track_summary(camera="feeder", track=t, num_frames=20)
    store.flush()
    tracks = store.query_tracks(species="Downy Woodpecker")
    assert len(tracks) == 1
    assert tracks[0]["species"] == "Downy Woodpecker"
    store.shutdown()


def test_query_tracks_filters(tmp_path):
    from pipeline.event_store import EventStore
    from pipeline.tracker import Track
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    for species, peak in [("Chickadee", 0.9), ("Cardinal", 0.7), ("Chickadee", 0.85)]:
        t = Track(track_id=0, created_at_ms=1000, last_updated_ms=6000,
                  bbox=[0,0,10,10], confidence=peak, species=species,
                  model_source="yard")
        store.write_track_summary(camera="feeder", track=t, num_frames=30)
    store.flush()

    chickadees = store.query_tracks(species="Chickadee")
    assert len(chickadees) == 2

    high_conf = store.query_tracks(min_confidence=0.8)
    assert len(high_conf) == 2  # Chickadee 0.9 and 0.85
    store.shutdown()


def test_prune_events_respects_age(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    now_ms = int(time.time() * 1000)
    # One old event, one recent
    store.write_event(camera="feeder", frame_time_ms=now_ms - 10 * 86400 * 1000,
                      track_id=1, species="Old", confidence=0.9,
                      model_source="yard", bbox=[0,0,10,10], is_new=True)
    store.write_event(camera="feeder", frame_time_ms=now_ms,
                      track_id=2, species="New", confidence=0.9,
                      model_source="yard", bbox=[0,0,10,10], is_new=True)
    store.flush()

    store.prune_events(older_than_ms=now_ms - 7 * 86400 * 1000)

    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT species FROM pipeline_events ORDER BY frame_time").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "New"
    store.shutdown()
