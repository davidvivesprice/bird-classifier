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
from typing import List, Optional

DB_PATH = Path.home() / "bird-snapshots" / "logs" / "classifications.db"

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
    conn.execute(CREATE_HISTORY_TABLE)
    for idx in HISTORY_INDEXES:
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

# ── Append-only history table (airtight 2026-04-24) ──
# Every review ever made lands here, never replaced, never deleted.
# The `reviews` table above is a current-state cache (latest row per file).
# Plan: docs/superpowers/specs/2026-04-23-airtight-review-system.md

CREATE_HISTORY_TABLE = """
CREATE TABLE IF NOT EXISTS review_history (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file             TEXT    NOT NULL,
    verdict          TEXT    NOT NULL,
    correct_species  TEXT    DEFAULT '',
    bird_index       INTEGER DEFAULT 0,
    missed_birds     INTEGER DEFAULT 0,
    reviewer         TEXT    DEFAULT 'dashboard',
    timestamp        TEXT    NOT NULL,
    prev_row_id      INTEGER,
    client_id        TEXT,
    FOREIGN KEY (prev_row_id) REFERENCES review_history(id)
)
"""

HISTORY_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_rh_file ON review_history(file)",
    "CREATE INDEX IF NOT EXISTS idx_rh_timestamp ON review_history(timestamp)",
    # Partial unique index: idempotency via client_id. NULL client_ids allowed
    # to co-exist (legacy path).
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_rh_client "
    "ON review_history(client_id) WHERE client_id IS NOT NULL",
]


# ── Write ──

INSERT_SQL = """
INSERT OR REPLACE INTO reviews (
    file, verdict, correct_species, bird_index, missed_birds, timestamp, reviewer
) VALUES (?, ?, ?, ?, ?, ?, ?)
"""

INSERT_HISTORY_SQL = """
INSERT INTO review_history (
    file, verdict, correct_species, bird_index, missed_birds,
    reviewer, timestamp, prev_row_id, client_id
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def insert_review(review_dict):
    """Insert a review. Appends to review_history (audit trail) AND upserts
    the current-state `reviews` row.

    Accepts a dict with keys:
      file, verdict, correct_species, bird_index, missed_birds,
      timestamp, reviewer, client_id.
    Missing keys get sensible defaults.

    If `client_id` is provided AND a history row with that client_id already
    exists, this call is a no-op (idempotent replay). Returns the existing
    history_id with duplicate=True. This is how the UI protects against
    double-submit without ever losing or overwriting a correction.

    Returns: {"history_id": int, "prev_row_id": int|None, "duplicate": bool}
    Backward-compat: the return value is new, but no existing caller
    inspects it (verified 2026-04-24 in grep), so this is safe.
    """
    d = review_dict
    client_id = d.get("client_id")
    conn = get_conn(readonly=False)

    # Idempotency gate
    if client_id:
        existing = conn.execute(
            "SELECT id FROM review_history WHERE client_id = ?", (client_id,)
        ).fetchone()
        if existing:
            prev = conn.execute(
                "SELECT prev_row_id FROM review_history WHERE id = ?",
                (existing[0],),
            ).fetchone()
            return {
                "history_id": existing[0],
                "prev_row_id": prev[0] if prev else None,
                "duplicate": True,
            }

    # Chain back to the most recent history row for this file
    prev_row = conn.execute(
        "SELECT id FROM review_history WHERE file = ? ORDER BY id DESC LIMIT 1",
        (d["file"],),
    ).fetchone()
    prev_row_id = prev_row[0] if prev_row else None

    # Single transaction: append history + upsert current state
    try:
        conn.execute("BEGIN")
        cur = conn.execute(INSERT_HISTORY_SQL, (
            d["file"], d["verdict"], d.get("correct_species", ""),
            d.get("bird_index", 0), int(d.get("missed_birds", 0)),
            d.get("reviewer", "dashboard"),
            d["timestamp"], prev_row_id, client_id,
        ))
        history_id = cur.lastrowid
        conn.execute(INSERT_SQL, (
            d["file"], d["verdict"], d.get("correct_species", ""),
            d.get("bird_index", 0), int(d.get("missed_birds", 0)),
            d["timestamp"], d.get("reviewer", "dashboard"),
        ))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {
        "history_id": history_id,
        "prev_row_id": prev_row_id,
        "duplicate": False,
    }


def get_history(file: str) -> List[dict]:
    """All review_history rows for `file`, oldest first. Empty list for
    files never reviewed.
    """
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT * FROM review_history WHERE file = ? ORDER BY id ASC",
        (file,),
    ).fetchall()
    return [dict(r) for r in rows]


def undo(history_id: int, client_id: Optional[str] = None) -> dict:
    """Undo the history row `history_id`. Appends a new history row with
    verdict='undone' that points back at it via prev_row_id, and rewrites
    the `reviews` cache to reflect the state BEFORE the undone entry.

    If there is no prior state (we're undoing the first review for a file),
    the `reviews` row is deleted. The history entry for 'undone' remains.

    Idempotent when `client_id` is provided.
    """
    from datetime import datetime
    conn = get_conn(readonly=False)

    # Idempotency
    if client_id:
        existing = conn.execute(
            "SELECT id FROM review_history WHERE client_id = ?", (client_id,)
        ).fetchone()
        if existing:
            return {"ok": True, "history_id": existing[0], "duplicate": True}

    target = conn.execute(
        "SELECT file, prev_row_id FROM review_history WHERE id = ?",
        (history_id,),
    ).fetchone()
    if target is None:
        return {"ok": False, "error": "history_id not found"}

    file = target["file"]
    prev_row_id = target["prev_row_id"]
    now = datetime.utcnow().isoformat()

    try:
        conn.execute("BEGIN")
        # Append 'undone' entry
        cur = conn.execute(INSERT_HISTORY_SQL, (
            file, "undone", "", 0, 0,
            "dashboard", now, history_id, client_id,
        ))
        undo_id = cur.lastrowid

        # Restore previous state OR delete reviews row
        if prev_row_id is not None:
            prev = conn.execute(
                "SELECT verdict, correct_species, bird_index, missed_birds, "
                "       timestamp, reviewer FROM review_history WHERE id = ?",
                (prev_row_id,),
            ).fetchone()
            conn.execute(INSERT_SQL, (
                file, prev["verdict"], prev["correct_species"] or "",
                prev["bird_index"] or 0, prev["missed_birds"] or 0,
                prev["timestamp"], prev["reviewer"],
            ))
        else:
            conn.execute("DELETE FROM reviews WHERE file = ?", (file,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return {"ok": True, "history_id": undo_id, "duplicate": False}


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


# DEPRECATED — use get_classifications(status="pending")
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


# DEPRECATED — use count_classifications(status="pending")
def count_pending(species=None, multibird=False):
    """Count pending (un-reviewed + requeued) classifications."""
    extra, params = _pending_where(species, multibird)
    sql = "SELECT COUNT(*) " + _PENDING_BASE + extra
    conn = get_conn(readonly=True)
    return conn.execute(sql, params).fetchone()[0]


# ── Review goals ──

def get_review_goals(regional_species, threshold=50, camera=None):
    """Compute per-species confirmed-review counts.

    Returns a list of dicts: {species, confirmed, complete}.
    *complete* is True when confirmed >= threshold.

    Counts come from two sources:
      1. 'correct' verdicts → species is c.common_name from the classification
      2. 'wrong' verdicts with a non-empty correct_species → species is r.correct_species

    If camera is set, only count confirmations from that camera.
    """
    conn = get_conn(readonly=True)

    cam_filter = ""
    cam_params = []
    if camera:
        cam_filter = " AND c.camera = ?"
        cam_params = [camera]

    # Source 1: correct verdicts
    rows_correct = conn.execute("""
        SELECT c.common_name AS species, COUNT(*) AS cnt
        FROM reviews r
        JOIN classifications c ON r.file = c.file
        WHERE r.verdict = 'correct'""" + cam_filter + """
        GROUP BY c.common_name
    """, cam_params).fetchall()

    # Source 2: wrong verdicts with corrected species
    rows_wrong = conn.execute("""
        SELECT r.correct_species AS species, COUNT(*) AS cnt
        FROM reviews r
        JOIN classifications c ON r.file = c.file
        WHERE r.verdict = 'wrong' AND r.correct_species != ''""" + cam_filter + """
        GROUP BY r.correct_species
    """, cam_params).fetchall()

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

# DEPRECATED — use get_classifications(status="reviewed")
def get_reviewed_entries(species=None, verdict=None, offset=0, limit=50):
    """Return reviewed classifications (JOIN reviews + classifications).

    Returns list of dicts combining review and classification fields.
    """
    conn = get_conn(readonly=True)

    where = []
    params = []
    if species:
        # Match the EFFECTIVE species: if corrected, use correct_species; otherwise use common_name
        # A corrected image belongs to the corrected species, not the original
        where.append("(CASE WHEN r.verdict = 'wrong' AND r.correct_species IS NOT NULL AND r.correct_species != '' "
                     "THEN r.correct_species ELSE c.common_name END) = ?")
        params.append(species)
    if verdict:
        where.append("r.verdict = ?")
        params.append(verdict)
    else:
        # When no specific verdict filter, exclude trashed items
        where.append("r.verdict NOT IN ('trash')")
        # Also exclude wrong→not_a_bird
        where.append("NOT (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird')")

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


def _reset_connections():
    """Reset all thread-local connections and table flag. For testing only."""
    global _table_ensured
    _table_ensured = False
    for attr in ("_reviews_ro_conn", "_reviews_rw_conn"):
        conn = getattr(_local, attr, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            delattr(_local, attr)


# ── Unified classification query system ──

_EFFECTIVE_SPECIES_SQL = """
CASE WHEN r.verdict = 'wrong' AND r.correct_species IS NOT NULL
     AND r.correct_species != '' AND r.correct_species != 'not_a_bird'
THEN r.correct_species
ELSE c.common_name
END
"""


def _build_classification_query(status, species=None, verdict=None,
                                 camera=None, date=None, multibird=False):
    """Build WHERE clause + params for unified classification queries."""
    where = ["c.action = 'classified'", "c.common_name IS NOT NULL"]
    params = []

    if status == "pending":
        where.append("(r.file IS NULL OR r.verdict = 'requeued')")
    elif status == "reviewed":
        where.append("r.file IS NOT NULL")
        where.append("r.verdict != 'requeued'")
        where.append("r.verdict NOT IN ('trash', 'reclassify')")
        where.append("NOT (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird')")
    elif status == "missed":
        # Missed birds — flagged for multi-bird reprocessing
        where.append("r.verdict = 'reclassify'")

    if species:
        where.append(f"({_EFFECTIVE_SPECIES_SQL}) = ?")
        params.append(species)

    if verdict and status not in ("pending", "missed"):
        where.append("r.verdict = ?")
        params.append(verdict)

    if camera:
        where.append("c.camera = ?")
        params.append(camera)

    if date:
        where.append("c.source_date = ?")
        params.append(date)

    # multibird tri-state:
    #   "exclude" → single-bird only (json_array_length <= 1, including NULL)
    #   any other truthy ("only" / True / non-empty str) → only multi-bird frames
    #   falsy ("" / None / False / 0) → no filter
    if multibird == "exclude":
        where.append("(c.birds_json IS NULL OR json_array_length(c.birds_json) <= 1)")
    elif multibird:
        where.append("json_array_length(c.birds_json) > 1")

    return " AND ".join(where), params


def get_classifications(status="pending", species=None, verdict=None,
                        camera=None, date=None, multibird=False,
                        offset=0, limit=50):
    """Unified query for all classification views.

    Args:
        status: "pending" (no review), "reviewed" (has review, not trash), "all"
        species: Filter by effective species (corrected if corrected, original otherwise)
        verdict: Filter by specific verdict (only for reviewed)
        camera: Filter by camera name
        date: Filter by source_date
        multibird: Only show multi-bird frames
        offset/limit: Pagination
    """
    where_clause, params = _build_classification_query(
        status, species, verdict, camera, date, multibird
    )

    # Order: pending by classification time, reviewed by review time
    order = "c.timestamp DESC" if status == "pending" else "COALESCE(r.timestamp, c.timestamp) DESC"

    sql = (
        f"SELECT c.file, c.common_name AS original_species, "
        f"({_EFFECTIVE_SPECIES_SQL}) AS species, "
        f"c.scientific_name, c.confidence, c.source_timestamp, c.source_date, "
        f"c.best_detection_json, c.top3_json, c.raw_top3_json, c.birds_json, "
        f"c.extra_json, c.camera, c.raw_score, "
        f"r.verdict, r.correct_species, r.missed_birds, r.bird_index, "
        f"r.timestamp AS review_timestamp, r.reviewer "
        f"FROM classifications c "
        f"LEFT JOIN reviews r ON c.file = r.file "
        f"WHERE {where_clause} "
        f"ORDER BY {order} "
        f"LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    conn = get_conn(readonly=True)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_classifications(status="pending", species=None, verdict=None,
                          camera=None, date=None, multibird=False):
    """Count classifications — same filters as get_classifications."""
    where_clause, params = _build_classification_query(
        status, species, verdict, camera, date, multibird
    )

    sql = (
        f"SELECT COUNT(*) "
        f"FROM classifications c "
        f"LEFT JOIN reviews r ON c.file = r.file "
        f"WHERE {where_clause}"
    )

    conn = get_conn(readonly=True)
    return conn.execute(sql, params).fetchone()[0]


def list_classification_species(status="reviewed", camera=None):
    """List distinct species for filter dropdowns.

    Returns both effective species (for corrected items) AND original species
    (for non-corrected items), so the dropdown includes all species the user
    might want to filter by.
    """
    where_clause, params = _build_classification_query(status, camera=camera)

    sql = (
        f"SELECT DISTINCT name FROM ("
        f"  SELECT ({_EFFECTIVE_SPECIES_SQL}) AS name "
        f"  FROM classifications c "
        f"  LEFT JOIN reviews r ON c.file = r.file "
        f"  WHERE {where_clause} "
        f"  UNION "
        f"  SELECT c.common_name AS name "
        f"  FROM classifications c "
        f"  LEFT JOIN reviews r ON c.file = r.file "
        f"  WHERE {where_clause} "
        f") ORDER BY name"
    )

    conn = get_conn(readonly=True)
    rows = conn.execute(sql, params + params).fetchall()
    return [r[0] for r in rows if r[0]]
