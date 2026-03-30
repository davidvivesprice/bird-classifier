"""
reviews_db — SQLite interface for the bird review workflow.

Stores review verdicts (correct / wrong / unsure / requeued) in the same
DB as classifications.  Provides JOIN queries for the pending-review and
review-goals dashboards.

Thread-safe: uses thread-local connections, WAL mode (same as classifications_db).
"""

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path("/Users/vives/bird-snapshots/logs/classifications.db")

# ── Connection pool (thread-local) ──

_local = threading.local()
_table_ensured = False
_table_lock = threading.RLock()  # RLock: reentrant because _ensure_table calls get_conn recursively


def get_conn(readonly=False):
    """Get a thread-local SQLite connection. Ensures reviews table exists on first call."""
    global _table_ensured
    attr = "_reviews_ro_conn" if readonly else "_reviews_rw_conn"
    conn = getattr(_local, attr, None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            if _table_ensured:
                return conn
        except sqlite3.Error:
            conn = None

    uri = f"file:{DB_PATH}"
    if readonly:
        uri += "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if readonly:
        conn.execute("PRAGMA query_only=ON")
    setattr(_local, attr, conn)

    if not _table_ensured:
        with _table_lock:
            if not _table_ensured:
                _ensure_table(conn, readonly)

    return conn


def _ensure_table(conn, readonly):
    """Create the reviews table and indexes if they don't exist."""
    global _table_ensured
    if readonly:
        # Can't create tables on a read-only connection; try a separate rw conn
        rw = get_conn(readonly=False)
        _ensure_table(rw, False)
        return
    conn.execute(CREATE_TABLE)
    for idx in INDEXES:
        conn.execute(idx)
    conn.commit()
    _table_ensured = True


# ── Schema ──

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS reviews (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file             TEXT    UNIQUE NOT NULL,
    verdict          TEXT    NOT NULL,
    correct_species  TEXT    DEFAULT '',
    bird_index       INTEGER DEFAULT 0,
    missed_birds     INTEGER DEFAULT 0,
    timestamp        TEXT    NOT NULL,
    reviewer         TEXT    DEFAULT 'dashboard'
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_reviews_file ON reviews(file)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_verdict ON reviews(verdict)",
    "CREATE INDEX IF NOT EXISTS idx_reviews_species ON reviews(correct_species)",
]


# ── Write ──

INSERT_SQL = """
INSERT OR REPLACE INTO reviews (
    file, verdict, correct_species, bird_index, missed_birds, timestamp, reviewer
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def insert_review(review_dict):
    """Insert or replace a review entry.

    Accepts a dict with keys: file, verdict, correct_species, bird_index,
    missed_birds, timestamp, reviewer.  Missing keys get sensible defaults.
    """
    d = review_dict
    row = (
        d["file"],
        d["verdict"],
        d.get("correct_species", ""),
        d.get("bird_index", 0),
        d.get("missed_birds", 0),
        d["timestamp"],
        d.get("reviewer", "dashboard"),
    )
    conn = get_conn(readonly=False)
    conn.execute(INSERT_SQL, row)
    conn.commit()


# ── Read helpers ──

def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def get_review(filename):
    """Return the review for *filename* as a dict, or None."""
    conn = get_conn(readonly=True)
    row = conn.execute("SELECT * FROM reviews WHERE file=?", (filename,)).fetchone()
    return _row_to_dict(row) if row else None


def get_all_reviews():
    """Return all reviews as a dict keyed by filename (backward-compat)."""
    conn = get_conn(readonly=True)
    rows = conn.execute("SELECT * FROM reviews").fetchall()
    return {r["file"]: _row_to_dict(r) for r in rows}


def count_reviews():
    """Return the total number of reviews."""
    conn = get_conn(readonly=True)
    return conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]


def get_reviews_by_verdict(verdict):
    """Return list of review dicts matching the given verdict."""
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT * FROM reviews WHERE verdict=?", (verdict,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


# ── Pending classifications (LEFT JOIN) ──

_PENDING_BASE = """
FROM classifications c
LEFT JOIN reviews r ON c.file = r.file
WHERE c.action = 'classified'
  AND c.common_name IS NOT NULL
  AND (r.file IS NULL OR r.verdict = 'requeued')
"""


def _pending_where(species=None, multibird=False):
    """Build extra WHERE clauses and params for pending queries."""
    extra = ""
    params = []
    if species:
        extra += " AND c.common_name = ?"
        params.append(species)
    if multibird:
        extra += " AND json_array_length(c.birds_json) > 1"
    return extra, params


def get_pending_classifications(species=None, multibird=False, offset=0, limit=50):
    """Return pending (un-reviewed + requeued) classifications.

    Performs a LEFT JOIN so only classifications without a review
    (or with verdict='requeued') are returned.
    """
    extra, params = _pending_where(species, multibird)
    sql = (
        "SELECT c.file, c.common_name, c.scientific_name, c.confidence, "
        "c.source_timestamp, c.source_date, c.best_detection_json, c.top3_json, "
        "c.raw_top3_json, c.birds_json, c.extra_json, c.camera, c.raw_score "
        + _PENDING_BASE + extra
        + " ORDER BY c.timestamp DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    conn = get_conn(readonly=True)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_pending(species=None, multibird=False):
    """Count pending (un-reviewed + requeued) classifications."""
    extra, params = _pending_where(species, multibird)
    sql = "SELECT COUNT(*) " + _PENDING_BASE + extra
    conn = get_conn(readonly=True)
    return conn.execute(sql, params).fetchone()[0]


# ── Review goals ──

def get_review_goals(regional_species, threshold=20):
    """Compute per-species confirmed-review counts.

    Returns a list of dicts: {species, confirmed, complete}.
    *complete* is True when confirmed >= threshold.

    Counts come from two sources:
      1. 'correct' verdicts → species is c.common_name from the classification
      2. 'wrong' verdicts with a non-empty correct_species → species is r.correct_species
    """
    conn = get_conn(readonly=True)

    # Source 1: correct verdicts
    rows_correct = conn.execute("""
        SELECT c.common_name AS species, COUNT(*) AS cnt
        FROM reviews r
        JOIN classifications c ON r.file = c.file
        WHERE r.verdict = 'correct'
        GROUP BY c.common_name
    """).fetchall()

    # Source 2: wrong verdicts with corrected species
    rows_wrong = conn.execute("""
        SELECT r.correct_species AS species, COUNT(*) AS cnt
        FROM reviews r
        WHERE r.verdict = 'wrong' AND r.correct_species != ''
        GROUP BY r.correct_species
    """).fetchall()

    counts = {}
    for row in rows_correct:
        counts[row["species"]] = counts.get(row["species"], 0) + row["cnt"]
    for row in rows_wrong:
        counts[row["species"]] = counts.get(row["species"], 0) + row["cnt"]

    result = []
    for sp in sorted(regional_species):
        confirmed = counts.get(sp, 0)
        result.append({
            "species": sp,
            "confirmed": confirmed,
            "complete": confirmed >= threshold,
        })
    return result


# ── Reviewed entries (JOIN for review/classified endpoint) ──

def get_reviewed_entries(species=None, verdict=None, offset=0, limit=50):
    """Return reviewed classifications (JOIN reviews + classifications).

    Returns list of dicts combining review and classification fields.
    """
    conn = get_conn(readonly=True)

    where = []
    params = []
    if species:
        where.append("c.common_name = ?")
        params.append(species)
    if verdict:
        where.append("r.verdict = ?")
        params.append(verdict)

    extra_where = (" AND " + " AND ".join(where)) if where else ""

    sql = (
        "SELECT r.file, r.verdict, r.correct_species, r.bird_index, "
        "r.missed_birds, r.timestamp AS review_timestamp, r.reviewer, "
        "c.common_name, c.scientific_name, c.confidence, "
        "c.source_timestamp, c.best_detection_json, c.top3_json, "
        "c.raw_top3_json, c.birds_json, c.camera, c.raw_score "
        "FROM reviews r "
        "JOIN classifications c ON r.file = c.file "
        "WHERE 1=1" + extra_where +
        " ORDER BY r.timestamp DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# ── Reset helper (for testing) ──

def _reset_table_flag():
    """Reset the table-ensured flag. Only used in tests."""
    global _table_ensured
    _table_ensured = False
