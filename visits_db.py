"""
visits_db — SQLite interface for the visit-based event model.

A "visit" groups consecutive detections of the same species on the same camera
into a single event.  A visit ends when no detection arrives for a configurable
gap (default 60 s).

Used by:
  - classify.py  (start_visit, extend_visit, get_active_visit)
  - api.py       (query / summary functions)

Thread-safe: uses thread-local connections, WAL mode (same as classifications_db).
"""

import sqlite3
import threading
from pathlib import Path

DB_PATH = Path("/Users/vives/bird-snapshots/logs/classifications.db")

DEFAULT_GAP_SECONDS = 60  # Visit ends after 60s with no detection


# ── Connection pool (thread-local) ──

_local = threading.local()
_table_ensured = False
_table_lock = threading.RLock()  # RLock: reentrant because _ensure_table calls get_conn recursively


def get_conn(readonly=False):
    """Get a thread-local SQLite connection. Ensures visits table exists on first call."""
    global _table_ensured
    attr = "_visits_ro_conn" if readonly else "_visits_rw_conn"
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
            if not _table_ensured:  # double-check after acquiring lock
                _ensure_table(conn, readonly)

    return conn


def _ensure_table(conn, readonly):
    """Create the visits table and indexes if they don't exist."""
    global _table_ensured
    if readonly:
        rw = get_conn(readonly=False)
        _ensure_table(rw, False)
        return
    ensure_visits_table(conn)
    _table_ensured = True


# ── Schema ──

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS visits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    camera          TEXT    NOT NULL,
    species         TEXT    NOT NULL,
    scientific_name TEXT,
    start_time      TEXT    NOT NULL,
    end_time        TEXT,
    status          TEXT    DEFAULT 'active',
    duration_sec    REAL    DEFAULT 0,
    frame_count     INTEGER DEFAULT 1,
    best_confidence REAL,
    best_score      REAL,
    best_file       TEXT,
    avg_confidence  REAL,
    bird_count      INTEGER DEFAULT 1,
    source_date     TEXT    NOT NULL
)
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_visits_date ON visits(source_date)",
    "CREATE INDEX IF NOT EXISTS idx_visits_species ON visits(species)",
    "CREATE INDEX IF NOT EXISTS idx_visits_date_species ON visits(source_date, species)",
    "CREATE INDEX IF NOT EXISTS idx_visits_status ON visits(status)",
    "CREATE INDEX IF NOT EXISTS idx_visits_camera_species_status ON visits(camera, species, status)",
]


def ensure_visits_table(conn):
    """Create visits table and indexes if they don't exist."""
    conn.execute(CREATE_TABLE)
    for idx in INDEXES:
        conn.execute(idx)
    conn.commit()


# ── Write (visit lifecycle) ──

def start_visit(camera, species, scientific_name, timestamp, source_date,
                confidence, score, snapshot, bird_count=1):
    """Start a new visit. Returns visit_id."""
    conn = get_conn(readonly=False)
    cur = conn.execute(
        "INSERT INTO visits "
        "(camera, species, scientific_name, start_time, end_time, status, "
        " duration_sec, frame_count, best_confidence, best_score, best_file, "
        " avg_confidence, bird_count, source_date) "
        "VALUES (?, ?, ?, ?, ?, 'active', 0, 1, ?, ?, ?, ?, ?, ?)",
        (camera, species, scientific_name, timestamp, timestamp,
         confidence, score, snapshot, confidence, bird_count, source_date),
    )
    conn.commit()
    return cur.lastrowid


def extend_visit(visit_id, timestamp, confidence, score, snapshot, bird_count=1):
    """Add a frame to an existing visit.

    Increments frame_count, updates end_time.
    Updates best_confidence/best_score/best_file if this frame is better.
    Updates bird_count to peak (max seen in any single frame — e.g. 5 doves).
    Recalculates avg_confidence.
    """
    conn = get_conn(readonly=False)
    row = conn.execute(
        "SELECT frame_count, best_confidence, best_score, avg_confidence, bird_count "
        "FROM visits WHERE id=?",
        (visit_id,),
    ).fetchone()
    if row is None:
        return

    old_count = row["frame_count"]
    old_best_conf = row["best_confidence"]
    old_best_score = row["best_score"]
    old_avg = row["avg_confidence"] or 0.0
    old_bird_count = row["bird_count"] or 1

    new_count = old_count + 1
    new_avg = (old_avg * old_count + (confidence or 0.0)) / new_count
    # Track peak bird count (most individuals seen in a single frame)
    new_bird_count = max(old_bird_count, bird_count or 1)

    # Update best if this frame has higher confidence
    new_best_conf = old_best_conf
    new_best_score = old_best_score
    new_snapshot = None
    if confidence is not None and (old_best_conf is None or confidence > old_best_conf):
        new_best_conf = confidence
        new_best_score = score
        new_snapshot = snapshot

    if new_snapshot is not None:
        conn.execute(
            "UPDATE visits SET end_time=?, "
            "duration_sec=(julianday(?) - julianday(start_time)) * 86400, "
            "frame_count=?, bird_count=?, "
            "best_confidence=?, best_score=?, best_file=?, avg_confidence=? "
            "WHERE id=?",
            (timestamp, timestamp, new_count, new_bird_count, new_best_conf,
             new_best_score, new_snapshot, new_avg, visit_id),
        )
    else:
        conn.execute(
            "UPDATE visits SET end_time=?, "
            "duration_sec=(julianday(?) - julianday(start_time)) * 86400, "
            "frame_count=?, bird_count=?, avg_confidence=? "
            "WHERE id=?",
            (timestamp, timestamp, new_count, new_bird_count, new_avg, visit_id),
        )
    conn.commit()


def end_visit(visit_id):
    """Mark visit as ended. Sets status='ended'."""
    conn = get_conn(readonly=False)
    conn.execute(
        "UPDATE visits SET status='ended' WHERE id=?",
        (visit_id,),
    )
    conn.commit()


# ── Read (active visit lookup) ──

def get_active_visit(camera, species, current_time, gap_seconds=DEFAULT_GAP_SECONDS):
    """Find an active visit for this camera+species within gap threshold.

    Returns dict with visit data or None if no active visit within gap.
    """
    conn = get_conn(readonly=True)
    row = conn.execute(
        "SELECT * FROM visits "
        "WHERE camera=? AND species=? AND status='active' "
        "AND (julianday(?) - julianday(end_time)) * 86400 <= ? "
        "ORDER BY end_time DESC LIMIT 1",
        (camera, species, current_time, gap_seconds),
    ).fetchone()
    return dict(row) if row else None


def end_stale_visits(max_age_seconds=300):
    """End all active visits older than max_age_seconds. For crash recovery."""
    conn = get_conn(readonly=False)
    conn.execute(
        "UPDATE visits SET status='ended' "
        "WHERE status='active' "
        "AND (julianday('now', 'localtime') - julianday(end_time)) * 86400 > ?",
        (max_age_seconds,),
    )
    conn.commit()


# ── Read helpers (queries) ──

def _row_to_dict(row):
    """Convert a sqlite3.Row to a plain dict."""
    return dict(row)


def get_visits(date=None, camera=None, species=None, limit=50, offset=0):
    """Query visits with optional filters. Returns list of dicts."""
    conn = get_conn(readonly=True)

    where = []
    params = []
    if date:
        where.append("source_date=?")
        params.append(date)
    if camera:
        where.append("camera=?")
        params.append(camera)
    if species:
        where.append("species=?")
        params.append(species)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = (
        f"SELECT * FROM visits {where_clause} "
        "ORDER BY start_time DESC LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def count_visits(date=None, camera=None, species=None):
    """Count visits with optional filters."""
    conn = get_conn(readonly=True)

    where = []
    params = []
    if date:
        where.append("source_date=?")
        params.append(date)
    if camera:
        where.append("camera=?")
        params.append(camera)
    if species:
        where.append("species=?")
        params.append(species)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT COUNT(*) FROM visits {where_clause}"
    return conn.execute(sql, params).fetchone()[0]


def get_visit_summary(date):
    """Species visit counts for a date.

    Returns: [{species, visits, frames, avg_duration_seconds, peak_confidence}, ...]
    """
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT species, "
        "       COUNT(*) AS visits, "
        "       SUM(frame_count) AS frames, "
        "       AVG((julianday(end_time) - julianday(start_time)) * 86400) AS avg_duration_seconds, "
        "       MAX(best_confidence) AS peak_confidence "
        "FROM visits "
        "WHERE source_date=? "
        "GROUP BY species "
        "ORDER BY visits DESC",
        (date,),
    ).fetchall()
    return [
        {
            "species": r["species"],
            "visits": r["visits"],
            "frames": r["frames"],
            "avg_duration_seconds": round(r["avg_duration_seconds"], 1) if r["avg_duration_seconds"] else 0,
            "peak_confidence": round(r["peak_confidence"], 3) if r["peak_confidence"] else 0,
        }
        for r in rows
    ]


def get_visit_stats(date):
    """Aggregate stats: total_visits, total_frames, compression_ratio, species_count."""
    conn = get_conn(readonly=True)
    row = conn.execute(
        "SELECT COUNT(*) AS total_visits, "
        "       COALESCE(SUM(frame_count), 0) AS total_frames, "
        "       COUNT(DISTINCT species) AS species_count "
        "FROM visits "
        "WHERE source_date=?",
        (date,),
    ).fetchone()
    total_visits = row["total_visits"]
    total_frames = row["total_frames"]
    compression_ratio = round(total_frames / total_visits, 1) if total_visits > 0 else 0
    return {
        "total_visits": total_visits,
        "total_frames": total_frames,
        "compression_ratio": compression_ratio,
        "species_count": row["species_count"],
    }


# ── Reset helper (for testing) ──

def _reset_table_flag():
    """Reset the table-ensured flag. Only used in tests."""
    global _table_ensured
    _table_ensured = False
