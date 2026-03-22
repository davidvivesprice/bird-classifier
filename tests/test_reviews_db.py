"""
Tests for reviews_db — SQLite interface for the reviews table.

Uses in-memory SQLite so no real DB is touched.
"""

import sqlite3
import threading
import pytest

import reviews_db


# ── Fixtures ──

@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Patch reviews_db to use a shared in-memory SQLite database.

    Creates both the classifications and reviews tables so JOIN queries work.
    """
    # Shared in-memory DB (named so multiple connections see the same data)
    uri = "file:test_reviews?mode=memory&cache=shared"

    # Set up the schema via a "setup" connection
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

    # Reset the table-ensured flag so each test re-creates the reviews table
    reviews_db._reset_table_flag()

    # Patch get_conn to return connections to the in-memory DB
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

        # Ensure reviews table exists
        conn.execute(reviews_db.CREATE_TABLE)
        for idx in reviews_db.INDEXES:
            conn.execute(idx)
        conn.commit()
        return conn

    monkeypatch.setattr(reviews_db, "get_conn", fake_get_conn)

    yield setup_conn

    # Cleanup: drop tables
    setup_conn.execute("DROP TABLE IF EXISTS reviews")
    setup_conn.execute("DROP TABLE IF EXISTS classifications")
    setup_conn.commit()
    setup_conn.close()


def _insert_classification(conn, file, common_name, action="classified",
                           timestamp="2025-01-15T10:00:00", birds_json=None):
    """Helper to insert a classification row for testing."""
    conn.execute(
        "INSERT OR REPLACE INTO classifications "
        "(file, camera, timestamp, action, common_name, scientific_name, birds_json) "
        "VALUES (?, 'feeder', ?, ?, ?, 'Testus scientificus', ?)",
        (file, timestamp, action, common_name, birds_json),
    )
    conn.commit()


# ── Tests ──

class TestTableCreation:
    def test_reviews_table_exists(self, in_memory_db):
        conn = reviews_db.get_conn(readonly=True)
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='reviews'"
        ).fetchone()
        assert row is not None

    def test_reviews_indexes_exist(self, in_memory_db):
        conn = reviews_db.get_conn(readonly=True)
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_reviews%'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_reviews_file" in names
        assert "idx_reviews_verdict" in names
        assert "idx_reviews_species" in names


class TestInsertAndRetrieve:
    def test_insert_and_get(self, in_memory_db):
        reviews_db.insert_review({
            "file": "img001.jpg",
            "verdict": "correct",
            "timestamp": "2025-01-15T12:00:00",
        })
        r = reviews_db.get_review("img001.jpg")
        assert r is not None
        assert r["verdict"] == "correct"
        assert r["correct_species"] == ""
        assert r["reviewer"] == "dashboard"

    def test_insert_with_all_fields(self, in_memory_db):
        reviews_db.insert_review({
            "file": "img002.jpg",
            "verdict": "wrong",
            "correct_species": "Blue Jay",
            "bird_index": 1,
            "missed_birds": 2,
            "timestamp": "2025-01-15T12:05:00",
            "reviewer": "alice",
        })
        r = reviews_db.get_review("img002.jpg")
        assert r["correct_species"] == "Blue Jay"
        assert r["bird_index"] == 1
        assert r["missed_birds"] == 2
        assert r["reviewer"] == "alice"

    def test_get_nonexistent(self, in_memory_db):
        assert reviews_db.get_review("does_not_exist.jpg") is None


class TestUpsert:
    def test_upsert_same_file(self, in_memory_db):
        reviews_db.insert_review({
            "file": "img_upsert.jpg",
            "verdict": "correct",
            "timestamp": "2025-01-15T12:00:00",
        })
        reviews_db.insert_review({
            "file": "img_upsert.jpg",
            "verdict": "wrong",
            "correct_species": "Cardinal",
            "timestamp": "2025-01-15T13:00:00",
        })
        r = reviews_db.get_review("img_upsert.jpg")
        assert r["verdict"] == "wrong"
        assert r["correct_species"] == "Cardinal"
        # Should still be only 1 row
        assert reviews_db.count_reviews() == 1


class TestGetAllReviews:
    def test_returns_dict_keyed_by_file(self, in_memory_db):
        reviews_db.insert_review({
            "file": "a.jpg", "verdict": "correct", "timestamp": "2025-01-15T10:00:00"
        })
        reviews_db.insert_review({
            "file": "b.jpg", "verdict": "wrong", "timestamp": "2025-01-15T10:01:00"
        })
        all_reviews = reviews_db.get_all_reviews()
        assert isinstance(all_reviews, dict)
        assert "a.jpg" in all_reviews
        assert "b.jpg" in all_reviews
        assert all_reviews["a.jpg"]["verdict"] == "correct"


class TestCountReviews:
    def test_count_empty(self, in_memory_db):
        assert reviews_db.count_reviews() == 0

    def test_count_after_inserts(self, in_memory_db):
        reviews_db.insert_review({
            "file": "c1.jpg", "verdict": "correct", "timestamp": "2025-01-15T10:00:00"
        })
        reviews_db.insert_review({
            "file": "c2.jpg", "verdict": "wrong", "timestamp": "2025-01-15T10:01:00"
        })
        assert reviews_db.count_reviews() == 2


class TestGetReviewsByVerdict:
    def test_filter_by_verdict(self, in_memory_db):
        reviews_db.insert_review({
            "file": "v1.jpg", "verdict": "correct", "timestamp": "2025-01-15T10:00:00"
        })
        reviews_db.insert_review({
            "file": "v2.jpg", "verdict": "wrong", "timestamp": "2025-01-15T10:01:00"
        })
        reviews_db.insert_review({
            "file": "v3.jpg", "verdict": "correct", "timestamp": "2025-01-15T10:02:00"
        })
        correct = reviews_db.get_reviews_by_verdict("correct")
        assert len(correct) == 2
        wrong = reviews_db.get_reviews_by_verdict("wrong")
        assert len(wrong) == 1


class TestPendingClassifications:
    def test_returns_unreviewed(self, in_memory_db):
        _insert_classification(in_memory_db, "p1.jpg", "Robin")
        _insert_classification(in_memory_db, "p2.jpg", "Sparrow")
        pending = reviews_db.get_pending_classifications()
        files = [r["file"] for r in pending]
        assert "p1.jpg" in files
        assert "p2.jpg" in files

    def test_excludes_reviewed(self, in_memory_db):
        _insert_classification(in_memory_db, "p3.jpg", "Robin")
        _insert_classification(in_memory_db, "p4.jpg", "Sparrow")
        reviews_db.insert_review({
            "file": "p3.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:00:00"
        })
        pending = reviews_db.get_pending_classifications()
        files = [r["file"] for r in pending]
        assert "p3.jpg" not in files
        assert "p4.jpg" in files

    def test_includes_requeued(self, in_memory_db):
        _insert_classification(in_memory_db, "p5.jpg", "Robin")
        reviews_db.insert_review({
            "file": "p5.jpg", "verdict": "requeued", "timestamp": "2025-01-15T12:00:00"
        })
        pending = reviews_db.get_pending_classifications()
        files = [r["file"] for r in pending]
        assert "p5.jpg" in files

    def test_excludes_non_classified(self, in_memory_db):
        _insert_classification(in_memory_db, "skip1.jpg", "Robin", action="skipped_no_bird")
        pending = reviews_db.get_pending_classifications()
        files = [r["file"] for r in pending]
        assert "skip1.jpg" not in files

    def test_excludes_null_common_name(self, in_memory_db):
        _insert_classification(in_memory_db, "null_sp.jpg", None, action="classified")
        pending = reviews_db.get_pending_classifications()
        files = [r["file"] for r in pending]
        assert "null_sp.jpg" not in files

    def test_species_filter(self, in_memory_db):
        _insert_classification(in_memory_db, "sf1.jpg", "Robin")
        _insert_classification(in_memory_db, "sf2.jpg", "Sparrow")
        pending = reviews_db.get_pending_classifications(species="Robin")
        files = [r["file"] for r in pending]
        assert "sf1.jpg" in files
        assert "sf2.jpg" not in files


class TestCountPending:
    def test_count_pending(self, in_memory_db):
        _insert_classification(in_memory_db, "cp1.jpg", "Robin")
        _insert_classification(in_memory_db, "cp2.jpg", "Sparrow")
        reviews_db.insert_review({
            "file": "cp1.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:00:00"
        })
        assert reviews_db.count_pending() == 1

    def test_count_pending_with_species(self, in_memory_db):
        _insert_classification(in_memory_db, "cps1.jpg", "Robin")
        _insert_classification(in_memory_db, "cps2.jpg", "Sparrow")
        _insert_classification(in_memory_db, "cps3.jpg", "Robin")
        assert reviews_db.count_pending(species="Robin") == 2
        assert reviews_db.count_pending(species="Sparrow") == 1


class TestReviewGoals:
    def test_counts_correct_and_wrong(self, in_memory_db):
        # 2 correct Robin, 1 wrong corrected to Robin
        _insert_classification(in_memory_db, "g1.jpg", "Robin")
        _insert_classification(in_memory_db, "g2.jpg", "Robin")
        _insert_classification(in_memory_db, "g3.jpg", "Sparrow")

        reviews_db.insert_review({
            "file": "g1.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:00:00"
        })
        reviews_db.insert_review({
            "file": "g2.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:01:00"
        })
        reviews_db.insert_review({
            "file": "g3.jpg", "verdict": "wrong",
            "correct_species": "Robin",
            "timestamp": "2025-01-15T12:02:00"
        })

        goals = reviews_db.get_review_goals({"Robin", "Sparrow"}, threshold=3)
        goals_by_sp = {g["species"]: g for g in goals}

        assert goals_by_sp["Robin"]["confirmed"] == 3  # 2 correct + 1 wrong→Robin
        assert goals_by_sp["Robin"]["complete"] is True
        assert goals_by_sp["Sparrow"]["confirmed"] == 0  # the Sparrow was "wrong"
        assert goals_by_sp["Sparrow"]["complete"] is False

    def test_empty_goals(self, in_memory_db):
        goals = reviews_db.get_review_goals({"Robin"}, threshold=5)
        assert len(goals) == 1
        assert goals[0]["confirmed"] == 0
        assert goals[0]["complete"] is False


class TestReviewedEntries:
    def test_returns_joined_data(self, in_memory_db):
        _insert_classification(in_memory_db, "re1.jpg", "Robin")
        reviews_db.insert_review({
            "file": "re1.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:00:00"
        })
        entries = reviews_db.get_reviewed_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e["file"] == "re1.jpg"
        assert e["verdict"] == "correct"
        assert e["common_name"] == "Robin"

    def test_filter_by_verdict(self, in_memory_db):
        _insert_classification(in_memory_db, "re2.jpg", "Robin")
        _insert_classification(in_memory_db, "re3.jpg", "Sparrow")
        reviews_db.insert_review({
            "file": "re2.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:00:00"
        })
        reviews_db.insert_review({
            "file": "re3.jpg", "verdict": "wrong", "timestamp": "2025-01-15T12:01:00"
        })
        correct = reviews_db.get_reviewed_entries(verdict="correct")
        assert len(correct) == 1
        assert correct[0]["file"] == "re2.jpg"

    def test_filter_by_species(self, in_memory_db):
        _insert_classification(in_memory_db, "re4.jpg", "Robin")
        _insert_classification(in_memory_db, "re5.jpg", "Sparrow")
        reviews_db.insert_review({
            "file": "re4.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:00:00"
        })
        reviews_db.insert_review({
            "file": "re5.jpg", "verdict": "correct", "timestamp": "2025-01-15T12:01:00"
        })
        robin_entries = reviews_db.get_reviewed_entries(species="Robin")
        assert len(robin_entries) == 1
        assert robin_entries[0]["common_name"] == "Robin"
