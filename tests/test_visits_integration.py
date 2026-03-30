"""Integration tests for visit-based event model."""
import pytest
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "bird-snapshots" / "logs" / "classifications.db"

@pytest.fixture
def real_db():
    if not DB_PATH.exists():
        pytest.skip("Production database not found")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()

def test_visits_table_exists(real_db):
    tables = real_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='visits'"
    ).fetchall()
    assert len(tables) == 1

def test_visits_have_data(real_db):
    count = real_db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    assert count > 1000  # We populated ~10K visits

def test_compression_ratio(real_db):
    """Visits should be significantly fewer than detections."""
    detections = real_db.execute(
        "SELECT COUNT(*) FROM classifications WHERE action='classified'"
    ).fetchone()[0]
    visits = real_db.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    ratio = detections / max(visits, 1)
    assert ratio > 1.0, f"Expected some compression, got ratio {ratio:.1f}"
    print(f"Compression ratio: {ratio:.1f}x ({detections} detections → {visits} visits)")

def test_all_visits_have_valid_species(real_db):
    """No null or empty species."""
    bad = real_db.execute(
        "SELECT COUNT(*) FROM visits WHERE species IS NULL OR species = ''"
    ).fetchone()[0]
    assert bad == 0

def test_all_visits_have_timestamps(real_db):
    bad = real_db.execute(
        "SELECT COUNT(*) FROM visits WHERE start_time IS NULL OR start_time = ''"
    ).fetchone()[0]
    assert bad == 0

def test_frame_counts_positive(real_db):
    bad = real_db.execute(
        "SELECT COUNT(*) FROM visits WHERE frame_count < 1"
    ).fetchone()[0]
    assert bad == 0

def test_mourning_dove_compressed(real_db):
    """Mourning Dove should have high compression (known from data analysis)."""
    det = real_db.execute(
        "SELECT COUNT(*) FROM classifications WHERE common_name='Mourning Dove' AND action='classified'"
    ).fetchone()[0]
    vis = real_db.execute(
        "SELECT COUNT(*) FROM visits WHERE species='Mourning Dove'"
    ).fetchone()[0]
    if det > 100 and vis > 0:
        ratio = det / vis
        assert ratio > 1.5, f"Mourning Dove compression only {ratio:.1f}x"

def test_visit_summary_query(real_db):
    """Visit summary aggregation works."""
    rows = real_db.execute("""
        SELECT species, COUNT(*) as visit_count, SUM(frame_count) as total_frames
        FROM visits
        GROUP BY species
        ORDER BY visit_count DESC
        LIMIT 5
    """).fetchall()
    assert len(rows) > 0
    for row in rows:
        assert row[1] > 0  # visit_count
        assert row[2] >= row[1]  # frames >= visits
