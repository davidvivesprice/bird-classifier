"""Tests for classifications_db — SQLite query functions.

Uses a shared in-memory SQLite database so no real DB is touched.
"""
import json
import sqlite3
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import classifications_db as cdb
import reviews_db as rdb


# ── Fixtures ──

@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Patch classifications_db and reviews_db to use a shared in-memory SQLite database."""
    uri = "file:test_cdb?mode=memory&cache=shared"

    # Create schema via a setup connection
    setup_conn = sqlite3.connect(uri, uri=True)
    setup_conn.row_factory = sqlite3.Row
    setup_conn.execute(cdb.CREATE_TABLE)
    for idx in cdb.INDEXES:
        setup_conn.execute(idx)
    setup_conn.commit()

    # Also create the reviews table (needed for JOIN queries)
    setup_conn.execute(rdb.CREATE_TABLE)
    for idx in rdb.INDEXES:
        setup_conn.execute(idx)
    setup_conn.commit()

    # Reset reviews table flag
    rdb._reset_table_flag()

    # Patch get_conn for both modules
    local = threading.local()

    def fake_cdb_get_conn(readonly=False):
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
        return conn

    def fake_rdb_get_conn(readonly=False):
        attr = "_test_rdb_ro" if readonly else "_test_rdb_rw"
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
        return conn

    monkeypatch.setattr(cdb, "get_conn", fake_cdb_get_conn)
    monkeypatch.setattr(rdb, "get_conn", fake_rdb_get_conn)

    yield setup_conn

    setup_conn.execute("DROP TABLE IF EXISTS reviews")
    setup_conn.execute("DROP TABLE IF EXISTS classifications")
    setup_conn.commit()
    setup_conn.close()


def _insert(conn, file, common_name, action="classified",
            timestamp="2026-03-28T10:00:00", source_date="2026-03-28",
            scientific_name="Testus scientificus", confidence=0.85,
            raw_score=200, camera="feeder"):
    """Helper to insert a classification row."""
    conn.execute(
        "INSERT OR REPLACE INTO classifications "
        "(file, camera, timestamp, source_timestamp, source_date, action, "
        "common_name, scientific_name, confidence, raw_score) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (file, camera, timestamp, timestamp, source_date, action,
         common_name, scientific_name, confidence, raw_score),
    )
    conn.commit()


def _insert_review(conn, file, verdict, correct_species=""):
    """Helper to insert a review row."""
    conn.execute(
        "INSERT OR REPLACE INTO reviews (file, verdict, correct_species, timestamp) "
        "VALUES (?, ?, ?, '2026-03-28T10:05:00')",
        (file, verdict, correct_species),
    )
    conn.commit()


# ── count_classified ──

class TestCountClassified:
    def test_empty_db_returns_zero(self):
        assert cdb.count_classified() == 0

    def test_counts_classified_entries(self, in_memory_db):
        _insert(in_memory_db, "img001.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img002.jpg", "Blue Jay")
        _insert(in_memory_db, "img003.jpg", "House Sparrow")
        assert cdb.count_classified() == 3

    def test_excludes_trashed_entries(self, in_memory_db):
        _insert(in_memory_db, "img010.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img011.jpg", "Blue Jay")
        _insert_review(in_memory_db, "img011.jpg", "trash")
        assert cdb.count_classified() == 1

    def test_excludes_not_a_bird(self, in_memory_db):
        _insert(in_memory_db, "img020.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img021.jpg", "House Sparrow")
        _insert_review(in_memory_db, "img021.jpg", "wrong", "not_a_bird")
        assert cdb.count_classified() == 1

    def test_does_not_exclude_correct_reviews(self, in_memory_db):
        _insert(in_memory_db, "img030.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img031.jpg", "Blue Jay")
        _insert_review(in_memory_db, "img031.jpg", "correct")
        assert cdb.count_classified() == 2

    def test_does_not_count_skipped_entries(self, in_memory_db):
        _insert(in_memory_db, "img040.jpg", "Northern Cardinal", action="classified")
        _insert(in_memory_db, "img041.jpg", None, action="skipped_no_bird")
        assert cdb.count_classified() == 1


# ── count_species ──

class TestCountSpecies:
    def test_empty_db_returns_zero(self):
        assert cdb.count_species() == 0

    def test_counts_distinct_species(self, in_memory_db):
        _insert(in_memory_db, "img050.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img051.jpg", "Blue Jay")
        _insert(in_memory_db, "img052.jpg", "Northern Cardinal")  # duplicate species
        assert cdb.count_species() == 2

    def test_excludes_trashed_from_species_count(self, in_memory_db):
        _insert(in_memory_db, "img060.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img061.jpg", "Blue Jay")
        # Trash all Blue Jay entries
        _insert_review(in_memory_db, "img061.jpg", "trash")
        assert cdb.count_species() == 1

    def test_excludes_null_common_name(self, in_memory_db):
        _insert(in_memory_db, "img070.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img071.jpg", None)  # no species
        assert cdb.count_species() == 1


# ── update_common_name ──

class TestUpdateCommonName:
    def test_updates_common_name(self, in_memory_db):
        _insert(in_memory_db, "img080.jpg", "House Sparrow")
        cdb.update_common_name("img080.jpg", "Northern Cardinal")

        conn = cdb.get_conn(readonly=True)
        row = conn.execute(
            "SELECT common_name FROM classifications WHERE file=?",
            ("img080.jpg",)
        ).fetchone()
        assert row["common_name"] == "Northern Cardinal"

    def test_update_nonexistent_file_no_error(self, in_memory_db):
        """Updating a nonexistent file should not raise."""
        cdb.update_common_name("ghost.jpg", "Blue Jay")  # should not raise


# ── get_stats ──

class TestGetStats:
    def test_returns_correct_structure(self, in_memory_db):
        _insert(in_memory_db, "img090.jpg", "Northern Cardinal")
        stats = cdb.get_stats()
        assert "total" in stats
        assert "classified" in stats
        assert "skipped" in stats
        assert "species_count" in stats
        assert "last_updated" in stats

    def test_counts_total_correctly(self, in_memory_db):
        _insert(in_memory_db, "img100.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img101.jpg", None, action="skipped_no_bird")
        _insert(in_memory_db, "img102.jpg", "Blue Jay")
        stats = cdb.get_stats()
        assert stats["total"] == 3

    def test_classified_excludes_trashed(self, in_memory_db):
        _insert(in_memory_db, "img110.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img111.jpg", "Blue Jay")
        _insert_review(in_memory_db, "img111.jpg", "trash")
        stats = cdb.get_stats()
        assert stats["classified"] == 1

    def test_species_count_correct(self, in_memory_db):
        _insert(in_memory_db, "img120.jpg", "Northern Cardinal")
        _insert(in_memory_db, "img121.jpg", "Blue Jay")
        _insert(in_memory_db, "img122.jpg", "Northern Cardinal")
        stats = cdb.get_stats()
        assert stats["species_count"] == 2

    def test_date_filter(self, in_memory_db):
        _insert(in_memory_db, "img130.jpg", "Northern Cardinal",
                source_date="2026-03-27", timestamp="2026-03-27T10:00:00")
        _insert(in_memory_db, "img131.jpg", "Blue Jay",
                source_date="2026-03-28", timestamp="2026-03-28T10:00:00")
        stats = cdb.get_stats(date="2026-03-28")
        assert stats["total"] == 1

    def test_camera_filter(self, in_memory_db):
        _insert(in_memory_db, "img140.jpg", "Northern Cardinal", camera="feeder")
        _insert(in_memory_db, "img141.jpg", "Blue Jay", camera="ground")
        stats = cdb.get_stats(camera="feeder")
        assert stats["total"] == 1

    def test_all_date_returns_everything(self, in_memory_db):
        _insert(in_memory_db, "img150.jpg", "Northern Cardinal",
                source_date="2026-03-27")
        _insert(in_memory_db, "img151.jpg", "Blue Jay",
                source_date="2026-03-28")
        stats = cdb.get_stats(date="all")
        assert stats["total"] == 2
