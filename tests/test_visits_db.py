"""
Tests for visits_db — SQLite interface for the visit-based event model.

Uses in-memory SQLite so no real DB is touched.
"""

import sqlite3
import threading
import pytest

import visits_db


# ── Fixtures ──

@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Patch visits_db to use a shared in-memory SQLite database.

    Creates both the classifications and visits tables so JOIN queries work.
    """
    uri = "file:test_visits?mode=memory&cache=shared"

    setup_conn = sqlite3.connect(uri, uri=True)
    setup_conn.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            file            TEXT    UNIQUE NOT NULL,
            camera          TEXT    NOT NULL DEFAULT 'feeder',
            timestamp       TEXT    NOT NULL,
            source_timestamp TEXT,
            source_date     TEXT,
            action          TEXT    NOT NULL,
            detect_ms       REAL,
            classify_ms     REAL,
            total_ms        REAL,
            detections      INTEGER DEFAULT 0,
            best_detection_json TEXT,
            top_prediction_json TEXT,
            top3_json       TEXT,
            raw_top3_json   TEXT,
            birds_json      TEXT,
            common_name     TEXT,
            scientific_name TEXT,
            raw_score       REAL,
            confidence      REAL,
            range_filter_applied INTEGER DEFAULT 0,
            original_species TEXT,
            filter_reason   TEXT,
            extra_json      TEXT
        )
    """)
    setup_conn.commit()

    visits_db._reset_table_flag()

    local = threading.local()

    def fake_get_conn(readonly=False):
        attr = "_test_ro" if readonly else "_test_rw"
        conn = getattr(local, attr, None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                conn = None

        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        setattr(local, attr, conn)

        # Ensure visits table exists
        visits_db.ensure_visits_table(conn)
        return conn

    monkeypatch.setattr(visits_db, "get_conn", fake_get_conn)

    yield setup_conn

    setup_conn.execute("DROP TABLE IF EXISTS visits")
    setup_conn.execute("DROP TABLE IF EXISTS classifications")
    setup_conn.commit()
    setup_conn.close()


# ── Tests ──

class TestTableCreation:
    def test_visits_table_exists(self, in_memory_db):
        conn = visits_db.get_conn(readonly=True)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='visits'"
        ).fetchone()
        assert row is not None

    def test_visits_indexes_exist(self, in_memory_db):
        conn = visits_db.get_conn(readonly=True)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_visits%'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_visits_date" in names
        assert "idx_visits_species" in names
        assert "idx_visits_date_species" in names
        assert "idx_visits_status" in names
        assert "idx_visits_camera_species_status" in names


class TestStartVisit:
    def test_returns_id(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        assert isinstance(vid, int)
        assert vid > 0

    def test_visit_retrievable(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits = visits_db.get_visits(date="2025-06-01")
        assert len(visits) == 1
        v = visits[0]
        assert v["id"] == vid
        assert v["camera"] == "feeder"
        assert v["species"] == "Robin"
        assert v["scientific_name"] == "Turdus migratorius"
        assert v["start_time"] == "2025-06-01T10:00:00"
        assert v["end_time"] == "2025-06-01T10:00:00"
        assert v["status"] == "active"
        assert v["frame_count"] == 1
        assert v["best_confidence"] == 0.85
        assert v["best_score"] == 0.9
        assert v["best_snapshot"] == "snap001.jpg"
        assert v["avg_confidence"] == 0.85
        assert v["bird_count"] == 1
        assert v["source_date"] == "2025-06-01"


class TestExtendVisit:
    def test_increments_frame_count(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.extend_visit(vid, "2025-06-01T10:00:30", 0.80, 0.85, "snap002.jpg")
        visits = visits_db.get_visits(date="2025-06-01")
        assert visits[0]["frame_count"] == 2

    def test_updates_end_time(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.extend_visit(vid, "2025-06-01T10:00:30", 0.80, 0.85, "snap002.jpg")
        visits = visits_db.get_visits(date="2025-06-01")
        assert visits[0]["end_time"] == "2025-06-01T10:00:30"

    def test_best_confidence_wins(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        # Lower confidence — should NOT update best
        visits_db.extend_visit(vid, "2025-06-01T10:00:30", 0.80, 0.85, "snap002.jpg")
        visits = visits_db.get_visits(date="2025-06-01")
        assert visits[0]["best_confidence"] == 0.85
        assert visits[0]["best_snapshot"] == "snap001.jpg"

        # Higher confidence — SHOULD update best
        visits_db.extend_visit(vid, "2025-06-01T10:01:00", 0.95, 0.98, "snap003.jpg")
        visits = visits_db.get_visits(date="2025-06-01")
        assert visits[0]["best_confidence"] == 0.95
        assert visits[0]["best_score"] == 0.98
        assert visits[0]["best_snapshot"] == "snap003.jpg"

    def test_avg_confidence_calculated_correctly(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.80, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.extend_visit(vid, "2025-06-01T10:00:30", 0.90, 0.95, "snap002.jpg")
        visits_db.extend_visit(vid, "2025-06-01T10:01:00", 0.70, 0.80, "snap003.jpg")
        visits = visits_db.get_visits(date="2025-06-01")
        # avg = (0.80 + 0.90 + 0.70) / 3 = 0.8
        assert abs(visits[0]["avg_confidence"] - 0.8) < 0.001


class TestEndVisit:
    def test_sets_status_ended(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.end_visit(vid)
        visits = visits_db.get_visits(date="2025-06-01")
        assert visits[0]["status"] == "ended"


class TestGetActiveVisit:
    def test_returns_visit_within_gap(self, in_memory_db):
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        active = visits_db.get_active_visit(
            "feeder", "Robin", "2025-06-01T10:00:30",
        )
        assert active is not None
        assert active["species"] == "Robin"

    def test_returns_none_when_gap_exceeded(self, in_memory_db):
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        # 120 seconds later, default gap is 60
        active = visits_db.get_active_visit(
            "feeder", "Robin", "2025-06-01T10:02:00",
        )
        assert active is None

    def test_returns_none_for_different_species(self, in_memory_db):
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        active = visits_db.get_active_visit(
            "feeder", "Sparrow", "2025-06-01T10:00:30",
        )
        assert active is None

    def test_returns_none_for_different_camera(self, in_memory_db):
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        active = visits_db.get_active_visit(
            "garden", "Robin", "2025-06-01T10:00:30",
        )
        assert active is None

    def test_returns_none_for_ended_visit(self, in_memory_db):
        vid = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.end_visit(vid)
        active = visits_db.get_active_visit(
            "feeder", "Robin", "2025-06-01T10:00:30",
        )
        assert active is None


class TestEndStaleVisits:
    def test_ends_old_active_visits(self, in_memory_db):
        # Create a visit with end_time far in the past
        conn = visits_db.get_conn(readonly=False)
        conn.execute(
            "INSERT INTO visits "
            "(camera, species, start_time, end_time, status, frame_count, "
            " best_confidence, avg_confidence, bird_count, source_date) "
            "VALUES ('feeder', 'Robin', '2025-06-01T08:00:00', '2025-06-01T08:00:00', "
            " 'active', 1, 0.85, 0.85, 1, '2025-06-01')"
        )
        conn.commit()

        # end_stale_visits uses julianday('now'), so this visit is definitely stale
        visits_db.end_stale_visits(max_age_seconds=300)

        visits = visits_db.get_visits(date="2025-06-01")
        assert visits[0]["status"] == "ended"


class TestGetVisits:
    def _seed_visits(self):
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.start_visit(
            camera="feeder", species="Sparrow", scientific_name="Passer domesticus",
            timestamp="2025-06-01T11:00:00", source_date="2025-06-01",
            confidence=0.75, score=0.8, snapshot="snap002.jpg",
        )
        visits_db.start_visit(
            camera="garden", species="Robin", scientific_name="Turdus migratorius",
            timestamp="2025-06-02T09:00:00", source_date="2025-06-02",
            confidence=0.90, score=0.95, snapshot="snap003.jpg",
        )

    def test_filter_by_date(self, in_memory_db):
        self._seed_visits()
        visits = visits_db.get_visits(date="2025-06-01")
        assert len(visits) == 2

    def test_filter_by_species(self, in_memory_db):
        self._seed_visits()
        visits = visits_db.get_visits(species="Robin")
        assert len(visits) == 2
        assert all(v["species"] == "Robin" for v in visits)

    def test_filter_by_camera(self, in_memory_db):
        self._seed_visits()
        visits = visits_db.get_visits(camera="garden")
        assert len(visits) == 1
        assert visits[0]["camera"] == "garden"

    def test_no_filter_returns_all(self, in_memory_db):
        self._seed_visits()
        visits = visits_db.get_visits()
        assert len(visits) == 3


class TestCountVisits:
    def test_count_all(self, in_memory_db):
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name=None,
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.start_visit(
            camera="feeder", species="Sparrow", scientific_name=None,
            timestamp="2025-06-01T11:00:00", source_date="2025-06-01",
            confidence=0.75, score=0.8, snapshot="snap002.jpg",
        )
        assert visits_db.count_visits() == 2

    def test_count_with_date(self, in_memory_db):
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name=None,
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name=None,
            timestamp="2025-06-02T10:00:00", source_date="2025-06-02",
            confidence=0.85, score=0.9, snapshot="snap002.jpg",
        )
        assert visits_db.count_visits(date="2025-06-01") == 1
        assert visits_db.count_visits(date="2025-06-02") == 1


class TestGetVisitSummary:
    def test_aggregation(self, in_memory_db):
        vid1 = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name=None,
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.extend_visit(vid1, "2025-06-01T10:01:00", 0.90, 0.95, "snap002.jpg")

        visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name=None,
            timestamp="2025-06-01T14:00:00", source_date="2025-06-01",
            confidence=0.80, score=0.85, snapshot="snap003.jpg",
        )

        visits_db.start_visit(
            camera="feeder", species="Sparrow", scientific_name=None,
            timestamp="2025-06-01T11:00:00", source_date="2025-06-01",
            confidence=0.75, score=0.8, snapshot="snap004.jpg",
        )

        summary = visits_db.get_visit_summary("2025-06-01")
        by_species = {s["species"]: s for s in summary}

        assert by_species["Robin"]["visits"] == 2
        assert by_species["Robin"]["frames"] == 3  # 2 + 1
        assert by_species["Robin"]["peak_confidence"] == 0.9
        assert by_species["Sparrow"]["visits"] == 1
        assert by_species["Sparrow"]["frames"] == 1


class TestGetVisitStats:
    def test_stats(self, in_memory_db):
        vid1 = visits_db.start_visit(
            camera="feeder", species="Robin", scientific_name=None,
            timestamp="2025-06-01T10:00:00", source_date="2025-06-01",
            confidence=0.85, score=0.9, snapshot="snap001.jpg",
        )
        visits_db.extend_visit(vid1, "2025-06-01T10:00:30", 0.80, 0.85, "snap002.jpg")
        visits_db.extend_visit(vid1, "2025-06-01T10:01:00", 0.90, 0.95, "snap003.jpg")

        visits_db.start_visit(
            camera="feeder", species="Sparrow", scientific_name=None,
            timestamp="2025-06-01T11:00:00", source_date="2025-06-01",
            confidence=0.75, score=0.8, snapshot="snap004.jpg",
        )

        stats = visits_db.get_visit_stats("2025-06-01")
        assert stats["total_visits"] == 2
        assert stats["total_frames"] == 4  # 3 + 1
        assert stats["compression_ratio"] == 2.0
        assert stats["species_count"] == 2
