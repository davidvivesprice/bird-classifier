"""Tests for unified classification query system."""
import sqlite3
import threading
import pytest

import reviews_db  # module-level import for _reset_connections


@pytest.fixture
def test_db(tmp_path, monkeypatch):
    """Create a test DB with known classifications + reviews.

    Monkeypatches get_conn so reviews_db uses the temp DB directly,
    avoiding URI readonly mode issues with WAL pragma.
    """
    db_path = str(tmp_path / "test.db")
    setup_conn = sqlite3.connect(db_path)
    setup_conn.execute("""CREATE TABLE IF NOT EXISTS classifications (
        file TEXT PRIMARY KEY, action TEXT, common_name TEXT,
        scientific_name TEXT, confidence REAL, source_timestamp TEXT,
        source_date TEXT, best_detection_json TEXT, top3_json TEXT,
        raw_top3_json TEXT, birds_json TEXT, extra_json TEXT,
        camera TEXT, raw_score REAL, timestamp TEXT
    )""")
    setup_conn.execute("""CREATE TABLE IF NOT EXISTS reviews (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        file             TEXT    UNIQUE NOT NULL,
        verdict          TEXT    NOT NULL,
        correct_species  TEXT    DEFAULT '',
        bird_index       INTEGER DEFAULT 0,
        missed_birds     INTEGER DEFAULT 0,
        timestamp        TEXT    NOT NULL,
        reviewer         TEXT    DEFAULT 'dashboard'
    )""")
    for idx in reviews_db.INDEXES:
        setup_conn.execute(idx)

    data = [
        # (file, species, conf, camera, timestamp, verdict, correct_species, review_ts)
        ("bird1.jpg", "Song Sparrow", 0.9, "feeder", "2026-03-30 10:00:00", "correct", "", "2026-03-30 11:00:00"),
        ("bird2.jpg", "Dark-eyed Junco", 0.85, "ground", "2026-03-30 10:01:00", "wrong", "Black-capped Chickadee", "2026-03-30 11:01:00"),
        ("bird3.jpg", "Hairy Woodpecker", 0.7, "feeder", "2026-03-30 10:02:00", "wrong", "Downy Woodpecker", "2026-03-30 11:02:00"),
        ("bird4.jpg", "Rock Pigeon", 0.6, "ground", "2026-03-30 10:03:00", "trash", "", "2026-03-30 11:03:00"),
        ("bird5.jpg", "House Finch", 0.95, "feeder", "2026-03-30 10:04:00", None, None, None),  # pending
        ("bird6.jpg", "Blue Jay", 0.8, "feeder", "2026-03-30 10:05:00", "wrong", "not_a_bird", "2026-03-30 11:05:00"),
        ("bird7.jpg", "European Starling", 0.5, "ground", "2026-03-30 10:06:00", "wrong", "", "2026-03-30 11:06:00"),  # wrong, no correction
        ("multi1.jpg", "Song Sparrow", 0.9, "feeder", "2026-03-30 10:07:00", None, None, None),  # pending, multi-bird
    ]

    for f, sp, conf, cam, ts, verdict, correct, rts in data:
        birds_json = '[{"species":"Song Sparrow"},{"species":"House Finch"}]' if f == "multi1.jpg" else '[]'
        setup_conn.execute(
            "INSERT INTO classifications (file, action, common_name, confidence, camera, source_timestamp, timestamp, birds_json) "
            "VALUES (?, 'classified', ?, ?, ?, ?, ?, ?)",
            (f, sp, conf, cam, ts, ts, birds_json)
        )
        if verdict:
            setup_conn.execute(
                "INSERT INTO reviews (file, verdict, correct_species, timestamp, reviewer) "
                "VALUES (?, ?, ?, ?, 'test')",
                (f, verdict, correct or "", rts)
            )

    setup_conn.commit()

    # Monkeypatch get_conn to use the temp DB
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
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        setattr(local, attr, conn)
        return conn

    monkeypatch.setattr(reviews_db, "get_conn", fake_get_conn)

    yield db_path

    setup_conn.close()


@pytest.fixture
def empty_db(tmp_path, monkeypatch):
    """Create an empty test DB with schema only."""
    db_path = str(tmp_path / "empty.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE classifications (
        file TEXT PRIMARY KEY, action TEXT, common_name TEXT,
        scientific_name TEXT, confidence REAL, source_timestamp TEXT,
        source_date TEXT, best_detection_json TEXT, top3_json TEXT,
        raw_top3_json TEXT, birds_json TEXT, extra_json TEXT,
        camera TEXT, raw_score REAL, timestamp TEXT
    )""")
    conn.execute("""CREATE TABLE reviews (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        file             TEXT    UNIQUE NOT NULL,
        verdict          TEXT    NOT NULL,
        correct_species  TEXT    DEFAULT '',
        bird_index       INTEGER DEFAULT 0,
        missed_birds     INTEGER DEFAULT 0,
        timestamp        TEXT    NOT NULL,
        reviewer         TEXT    DEFAULT 'dashboard'
    )""")
    conn.commit()

    local = threading.local()

    def fake_get_conn(readonly=False):
        attr = "_test_ro" if readonly else "_test_rw"
        c = getattr(local, attr, None)
        if c is not None:
            try:
                c.execute("SELECT 1")
                return c
            except sqlite3.Error:
                c = None
        c = sqlite3.connect(db_path, timeout=10)
        c.row_factory = sqlite3.Row
        setattr(local, attr, c)
        return c

    monkeypatch.setattr(reviews_db, "get_conn", fake_get_conn)

    yield db_path
    conn.close()


@pytest.fixture(autouse=True)
def reset_db_state():
    """Reset reviews_db state before each test."""
    yield
    reviews_db._reset_connections()


class TestGetClassifications:

    def test_pending_returns_only_unreviewed(self, test_db):
        results = reviews_db.get_classifications(status="pending")
        files = [r["file"] for r in results]
        assert "bird5.jpg" in files
        assert "multi1.jpg" in files
        assert "bird1.jpg" not in files  # reviewed
        assert "bird4.jpg" not in files  # trashed
        assert len(files) == 2

    def test_reviewed_excludes_trash_and_not_a_bird(self, test_db):
        results = reviews_db.get_classifications(status="reviewed")
        files = [r["file"] for r in results]
        assert "bird4.jpg" not in files  # trashed
        assert "bird6.jpg" not in files  # not_a_bird
        assert "bird1.jpg" in files      # correct
        assert "bird2.jpg" in files      # corrected
        assert "bird3.jpg" in files      # corrected
        assert "bird7.jpg" in files      # wrong, no correction (still reviewed)

    def test_effective_species(self, test_db):
        results = reviews_db.get_classifications(status="reviewed")
        by_file = {r["file"]: r for r in results}
        assert by_file["bird2.jpg"]["species"] == "Black-capped Chickadee"
        assert by_file["bird2.jpg"]["original_species"] == "Dark-eyed Junco"
        assert by_file["bird1.jpg"]["species"] == "Song Sparrow"
        assert by_file["bird1.jpg"]["original_species"] == "Song Sparrow"
        # Uncorrected wrong: species == original
        assert by_file["bird7.jpg"]["species"] == "European Starling"
        assert by_file["bird7.jpg"]["original_species"] == "European Starling"

    def test_species_filter_matches_effective(self, test_db):
        results = reviews_db.get_classifications(status="reviewed", species="Black-capped Chickadee")
        assert len(results) == 1
        assert results[0]["file"] == "bird2.jpg"

    def test_species_filter_excludes_corrected_away(self, test_db):
        results = reviews_db.get_classifications(status="reviewed", species="Dark-eyed Junco")
        files = [r["file"] for r in results]
        assert "bird2.jpg" not in files

    def test_multibird_filter_in_sql(self, test_db):
        results = reviews_db.get_classifications(status="pending", multibird=True)
        assert len(results) == 1
        assert results[0]["file"] == "multi1.jpg"

    def test_multibird_count_matches_get(self, test_db):
        items = reviews_db.get_classifications(status="pending", multibird=True)
        count = reviews_db.count_classifications(status="pending", multibird=True)
        assert count == len(items)

    def test_status_all_includes_everything(self, test_db):
        results = reviews_db.get_classifications(status="all")
        assert len(results) == 8  # all records

    def test_pending_has_null_verdict(self, test_db):
        results = reviews_db.get_classifications(status="pending")
        for r in results:
            assert r["verdict"] is None

    def test_response_has_required_fields(self, test_db):
        results = reviews_db.get_classifications(status="reviewed")
        for r in results:
            for field in ["file", "species", "original_species", "verdict",
                          "correct_species", "confidence", "source_timestamp", "camera"]:
                assert field in r

    def test_empty_db(self, empty_db):
        assert reviews_db.get_classifications(status="pending") == []
        assert reviews_db.count_classifications(status="pending") == 0
        assert reviews_db.list_classification_species(status="reviewed") == []


class TestCountClassifications:

    def test_count_matches_get_reviewed(self, test_db):
        items = reviews_db.get_classifications(status="reviewed")
        count = reviews_db.count_classifications(status="reviewed")
        assert count == len(items)

    def test_count_pending(self, test_db):
        count = reviews_db.count_classifications(status="pending")
        assert count == 2  # bird5 + multi1

    def test_count_with_species(self, test_db):
        count = reviews_db.count_classifications(status="reviewed", species="Black-capped Chickadee")
        assert count == 1


class TestListClassificationSpecies:

    def test_includes_effective_species(self, test_db):
        species = reviews_db.list_classification_species(status="reviewed")
        assert "Black-capped Chickadee" in species  # corrected TO
        assert "Downy Woodpecker" in species         # corrected TO
        assert "Song Sparrow" in species              # confirmed

    def test_excludes_trashed_species(self, test_db):
        species = reviews_db.list_classification_species(status="reviewed")
        assert "Rock Pigeon" not in species  # trashed

    def test_includes_original_species_via_union(self, test_db):
        species = reviews_db.list_classification_species(status="reviewed")
        # Original species of corrected items should ALSO be in the list
        # (via UNION) so users can filter by "what the AI called it"
        assert "Dark-eyed Junco" in species   # original of bird2
        assert "Hairy Woodpecker" in species   # original of bird3
