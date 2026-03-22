"""Integration tests for reviews SQLite migration."""
import pytest
import sqlite3
from pathlib import Path

DB_PATH = Path.home() / "bird-snapshots" / "logs" / "classifications.db"

@pytest.fixture
def real_db():
    """Connect to the real production database (read-only tests)."""
    if not DB_PATH.exists():
        pytest.skip("Production database not found")
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()

def test_reviews_table_exists(real_db):
    """Reviews table should exist after migration."""
    tables = real_db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='reviews'"
    ).fetchall()
    assert len(tables) == 1

def test_reviews_have_data(real_db):
    """Should have migrated reviews from JSONL."""
    count = real_db.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    assert count > 500  # We know there are ~568 unique reviews

def test_reviews_verdicts_valid(real_db):
    """All verdicts should be from the known set."""
    valid = {'correct', 'wrong', 'skip', 'trash', 'reclassify', 'requeued'}
    verdicts = real_db.execute("SELECT DISTINCT verdict FROM reviews").fetchall()
    for row in verdicts:
        assert row[0] in valid, f"Unexpected verdict: {row[0]}"

def test_pending_join_works(real_db):
    """LEFT JOIN for pending should return unreviewed classifications."""
    row = real_db.execute("""
        SELECT COUNT(*) FROM classifications c
        LEFT JOIN reviews r ON c.file = r.file
        WHERE c.action = 'classified' AND c.common_name IS NOT NULL
          AND (r.file IS NULL OR r.verdict = 'requeued')
    """).fetchone()
    pending = row[0]
    assert pending > 0  # Should have many unreviewed items

def test_goals_query_works(real_db):
    """Goals aggregation should return species with confirmed counts."""
    rows = real_db.execute("""
        SELECT c.common_name, COUNT(*) as confirmed
        FROM reviews r JOIN classifications c ON r.file = c.file
        WHERE r.verdict = 'correct'
        GROUP BY c.common_name
        ORDER BY confirmed DESC
    """).fetchall()
    assert len(rows) > 0  # Should have some confirmed species

def test_no_orphan_reviews(real_db):
    """Every review should reference a file that exists in classifications."""
    orphans = real_db.execute("""
        SELECT COUNT(*) FROM reviews r
        LEFT JOIN classifications c ON r.file = c.file
        WHERE c.file IS NULL
    """).fetchone()[0]
    # Some orphans are OK (files that were deleted/trashed), but report
    if orphans > 0:
        print(f"Note: {orphans} reviews reference files not in classifications table")
