"""
classifications_db — shared SQLite interface for the bird observatory.

Used by:
  - classify.py  (insert_classification)
  - api.py       (query functions)

Thread-safe: each call opens its own connection or uses a thread-local pool.
WAL mode allows concurrent reads + one writer without blocking.
"""

import json
import sqlite3
import threading
from pathlib import Path

from bird_inference import SPECIES_ALIASES, normalize_species

DB_PATH = Path("/Users/vives/bird-snapshots/logs/classifications.db")


# ── Connection pool (thread-local) ──

_local = threading.local()


def get_conn(readonly=False):
    """Get a thread-local SQLite connection."""
    attr = "_ro_conn" if readonly else "_rw_conn"
    conn = getattr(_local, attr, None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
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
    return conn


# ── Schema (for init / verification) ──

CREATE_TABLE = """
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
"""

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_cls_file ON classifications(file)",
    "CREATE INDEX IF NOT EXISTS idx_cls_action ON classifications(action)",
    "CREATE INDEX IF NOT EXISTS idx_cls_source_date ON classifications(source_date)",
    "CREATE INDEX IF NOT EXISTS idx_cls_common_name ON classifications(common_name)",
    "CREATE INDEX IF NOT EXISTS idx_cls_camera ON classifications(camera)",
    "CREATE INDEX IF NOT EXISTS idx_cls_action_date ON classifications(action, source_date)",
    "CREATE INDEX IF NOT EXISTS idx_cls_confidence ON classifications(confidence)",
    "CREATE INDEX IF NOT EXISTS idx_cls_timestamp ON classifications(timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_cls_action_common ON classifications(action, common_name)",
    "CREATE INDEX IF NOT EXISTS idx_cls_date_action_name ON classifications(source_date, action, common_name)",
]


def init_db():
    """Ensure the DB and table exist. Safe to call multiple times."""
    conn = get_conn(readonly=False)
    conn.execute(CREATE_TABLE)
    for idx in INDEXES:
        conn.execute(idx)
    conn.commit()


# ── Write (used by classify.py) ──

INSERT_SQL = """
INSERT OR REPLACE INTO classifications (
    file, camera, timestamp, source_timestamp, source_date, action,
    detect_ms, classify_ms, total_ms, detections,
    best_detection_json, top_prediction_json, top3_json, raw_top3_json, birds_json,
    common_name, scientific_name, raw_score, confidence,
    range_filter_applied, original_species, filter_reason, extra_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Known top-level fields — anything else goes into extra_json
_KNOWN_FIELDS = {
    "file", "camera", "timestamp", "source_timestamp", "action",
    "detect_ms", "classify_ms", "total_ms", "detections",
    "best_detection", "top_prediction", "top3", "raw_top3", "birds",
    "common_name", "scientific_name", "raw_score", "confidence",
    "range_filter_applied", "original_species", "filter_reason", "filter_flags",
}


def insert_classification(entry: dict):
    """Insert or replace a classification entry into SQLite.

    Accepts the same dict format as the JSONL entries.
    """
    e = entry

    # Normalize species
    tp = e.get("top_prediction")
    if tp and "common_name" in tp:
        tp["common_name"] = normalize_species(tp["common_name"])

    common_name = tp["common_name"] if tp and "common_name" in tp else None
    scientific_name = tp["scientific_name"] if tp and "scientific_name" in tp else None
    raw_score = tp["raw_score"] if tp and "raw_score" in tp else None

    bd = e.get("best_detection")
    confidence = bd.get("confidence") if bd else None

    source_ts = e.get("source_timestamp", "")
    source_date = source_ts[:10] if source_ts and len(source_ts) >= 10 else None

    extra = {k: v for k, v in e.items() if k not in _KNOWN_FIELDS}
    extra_json = json.dumps(extra) if extra else None

    row = (
        e.get("file", ""),
        e.get("camera", "feeder"),
        e.get("timestamp", ""),
        source_ts or None,
        source_date,
        e.get("action", ""),
        e.get("detect_ms"),
        e.get("classify_ms"),
        e.get("total_ms"),
        e.get("detections", 0),
        json.dumps(bd) if bd else None,
        json.dumps(tp) if tp else None,
        json.dumps(e["top3"]) if "top3" in e else None,
        json.dumps(e["raw_top3"]) if "raw_top3" in e else None,
        json.dumps(e["birds"]) if "birds" in e else None,
        common_name,
        scientific_name,
        raw_score,
        confidence,
        1 if e.get("range_filter_applied") else 0,
        e.get("original_species"),
        e.get("filter_reason"),
        extra_json,
    )

    conn = get_conn(readonly=False)
    conn.execute(INSERT_SQL, row)
    conn.commit()


def _safe_json(s):
    """Parse JSON string, returning None on error instead of crashing."""
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None


# ── Read helpers (used by api.py) ──

def _row_to_entry(row):
    """Reconstruct the original dict from a SQLite row.

    This produces the same shape that the JSONL-based code returned,
    so API response formats stay identical.  Applies species normalization
    to nested structures (birds, top3) just like the old _normalize_entry().
    """
    d = {
        "file": row["file"],
        "camera": row["camera"],
        "timestamp": row["timestamp"],
        "source_timestamp": row["source_timestamp"],
        "action": row["action"],
        "detect_ms": row["detect_ms"],
        "classify_ms": row["classify_ms"],
        "total_ms": row["total_ms"],
        "detections": row["detections"],
    }

    bd = _safe_json(row["best_detection_json"])
    if bd:
        d["best_detection"] = bd
    tp = _safe_json(row["top_prediction_json"])
    if tp:
        d["top_prediction"] = tp
        if "common_name" in tp:
            tp["common_name"] = normalize_species(tp["common_name"])
    top3 = _safe_json(row["top3_json"])
    if top3:
        d["top3"] = top3
        for t in top3:
            if "common_name" in t:
                t["common_name"] = normalize_species(t["common_name"])
    raw_top3 = _safe_json(row["raw_top3_json"])
    if raw_top3:
        d["raw_top3"] = raw_top3
    birds = _safe_json(row["birds_json"])
    if birds:
        d["birds"] = birds
        for b in birds:
            if "common_name" in b:
                b["common_name"] = normalize_species(b["common_name"])
            for t in b.get("top3", []):
                if "common_name" in t:
                    t["common_name"] = normalize_species(t["common_name"])

    if row["range_filter_applied"]:
        d["range_filter_applied"] = True
        if row["original_species"]:
            d["original_species"] = row["original_species"]
        if row["filter_reason"]:
            d["filter_reason"] = row["filter_reason"]

    extra = _safe_json(row["extra_json"])
    if extra and isinstance(extra, dict):
        d.update(extra)

    return d


def count_total():
    conn = get_conn(readonly=True)
    return conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]


def count_classified():
    conn = get_conn(readonly=True)
    return conn.execute(
        "SELECT COUNT(*) FROM classifications c "
        "WHERE c.action='classified' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM reviews r WHERE r.file = c.file "
        "  AND (r.verdict = 'trash' OR (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird'))"
        ")"
    ).fetchone()[0]


def count_by_action(action_prefix=None):
    conn = get_conn(readonly=True)
    if action_prefix:
        return conn.execute(
            "SELECT COUNT(*) FROM classifications WHERE action LIKE ?",
            (action_prefix + "%",)
        ).fetchone()[0]
    return count_total()


def get_entry_by_file(filename):
    conn = get_conn(readonly=True)
    row = conn.execute("SELECT * FROM classifications WHERE file=?", (filename,)).fetchone()
    return _row_to_entry(row) if row else None


def update_common_name(filename, new_species):
    """Update the common_name for a classification after a review correction."""
    conn = get_conn(readonly=False)
    conn.execute(
        "UPDATE classifications SET common_name = ? WHERE file = ?",
        (new_species, filename),
    )
    conn.commit()


def get_species_list(date=None, camera=None):
    """Return species summary: [{common_name, scientific_name, count, last_seen, avg_confidence, avg_score}]"""
    conn = get_conn(readonly=True)

    where = ["action='classified'", "common_name IS NOT NULL"]
    params = []

    if date and date != "all":
        where.append("source_date=?")
        params.append(date)
    if camera and camera != "all":
        where.append("camera=?")
        params.append(camera)

    sql = f"""
        SELECT common_name, scientific_name,
               COUNT(*) as count,
               MAX(COALESCE(source_timestamp, timestamp)) as last_seen,
               AVG(confidence) as avg_confidence,
               AVG(raw_score) as avg_score
        FROM classifications
        WHERE {' AND '.join(where)}
        GROUP BY common_name
        ORDER BY count DESC
    """
    rows = conn.execute(sql, params).fetchall()
    return [
        {
            "common_name": r["common_name"],
            "scientific_name": r["scientific_name"],
            "count": r["count"],
            "last_seen": r["last_seen"],
            "avg_confidence": round(r["avg_confidence"], 3) if r["avg_confidence"] else 0,
            "avg_score": round(r["avg_score"], 1) if r["avg_score"] else 0,
        }
        for r in rows
    ]


def get_stats(date=None, camera=None):
    """Return overall stats: {total, classified, skipped, species_count, last_updated}"""
    conn = get_conn(readonly=True)

    where = []
    params = []
    if date and date != "all":
        where.append("source_date=?")
        params.append(date)
    if camera and camera != "all":
        where.append("camera=?")
        params.append(camera)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""

    sql = f"""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN action='classified' THEN 1 ELSE 0 END) as classified_raw,
               SUM(CASE WHEN action LIKE 'skipped%%' THEN 1 ELSE 0 END) as skipped,
               MAX(timestamp) as last_updated
        FROM classifications
        {where_clause}
    """
    r = conn.execute(sql, params).fetchone()

    # Exclude trashed/not_a_bird from classified count and species count
    excluded = conn.execute(
        "SELECT COUNT(DISTINCT c.file) FROM classifications c "
        "JOIN reviews r ON r.file = c.file "
        "WHERE c.action='classified' "
        "AND (r.verdict = 'trash' OR (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird'))"
    ).fetchone()[0]

    species_count = conn.execute(
        "SELECT COUNT(DISTINCT c.common_name) FROM classifications c "
        "WHERE c.action='classified' AND c.common_name IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM reviews r WHERE r.file = c.file "
        "  AND (r.verdict = 'trash' OR (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird'))"
        ")"
    ).fetchone()[0]

    return {
        "total": r["total"],
        "classified": (r["classified_raw"] or 0) - excluded,
        "skipped": r["skipped"],
        "species_count": species_count,
        "last_updated": r["last_updated"],
    }


def get_recent(limit=20, date=None, camera=None):
    """Return recent classified entries."""
    conn = get_conn(readonly=True)

    where = ["action='classified'"]
    params = []
    if date and date != "all":
        where.append("source_date=?")
        params.append(date)
    if camera and camera != "all":
        where.append("camera=?")
        params.append(camera)

    sql = f"""
        SELECT * FROM classifications
        WHERE {' AND '.join(where)}
        ORDER BY timestamp DESC
        LIMIT ?
    """
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_entry(r) for r in rows]


def get_dates():
    """Return sorted list of dates with data, newest first."""
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT DISTINCT source_date FROM classifications WHERE source_date IS NOT NULL ORDER BY source_date DESC"
    ).fetchall()
    return [r["source_date"] for r in rows]


def get_cameras():
    """Return camera stats: [{name, count, last_seen}]"""
    conn = get_conn(readonly=True)
    rows = conn.execute("""
        SELECT camera, COUNT(*) as count,
               MAX(COALESCE(source_timestamp, timestamp)) as last_seen
        FROM classifications
        WHERE action='classified'
        GROUP BY camera
        ORDER BY camera
    """).fetchall()
    return [{"name": r["camera"], "count": r["count"], "last_seen": r["last_seen"]} for r in rows]


def get_species_entries(species_name, limit=200):
    """Return entries for a given species."""
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT * FROM classifications WHERE common_name=? ORDER BY timestamp DESC LIMIT ?",
        (species_name, limit)
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def get_entries_by_date(date):
    """Return all entries for a given date."""
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT * FROM classifications WHERE source_date=? ORDER BY timestamp",
        (date,)
    ).fetchall()
    return [_row_to_entry(r) for r in rows]


def get_classified_entries(date=None, camera=None):
    """Return all classified entries, optionally filtered."""
    conn = get_conn(readonly=True)

    where = ["action='classified'"]
    params = []
    if date and date != "all":
        where.append("source_date=?")
        params.append(date)
    if camera and camera != "all":
        where.append("camera=?")
        params.append(camera)

    sql = f"SELECT * FROM classifications WHERE {' AND '.join(where)} ORDER BY timestamp"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_entry(r) for r in rows]


def get_all_entries():
    """Return all entries. Use sparingly — for endpoints that need the full set."""
    conn = get_conn(readonly=True)
    rows = conn.execute("SELECT * FROM classifications ORDER BY timestamp").fetchall()
    return [_row_to_entry(r) for r in rows]


def get_entries_filtered(date=None, camera=None):
    """Return all entries filtered by date and/or camera."""
    conn = get_conn(readonly=True)

    where = []
    params = []
    if date and date != "all":
        where.append("source_date=?")
        params.append(date)
    if camera and camera != "all":
        where.append("camera=?")
        params.append(camera)

    where_clause = ("WHERE " + " AND ".join(where)) if where else ""
    sql = f"SELECT * FROM classifications {where_clause} ORDER BY timestamp"
    rows = conn.execute(sql, params).fetchall()
    return [_row_to_entry(r) for r in rows]


def file_exists(filename):
    """Check if a file has already been classified."""
    conn = get_conn(readonly=True)
    row = conn.execute("SELECT 1 FROM classifications WHERE file=?", (filename,)).fetchone()
    return row is not None


# ── Phase 2: Direct-query functions for api.py (replaces in-memory cache) ──

def count_species():
    """Count distinct classified species, excluding trashed/not_a_bird."""
    conn = get_conn(readonly=True)
    return conn.execute(
        "SELECT COUNT(DISTINCT c.common_name) FROM classifications c "
        "WHERE c.action='classified' AND c.common_name IS NOT NULL "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM reviews r WHERE r.file = c.file "
        "  AND (r.verdict = 'trash' OR (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird'))"
        ")"
    ).fetchone()[0]


def get_last_timestamp():
    """Return the most recent timestamp across all entries."""
    conn = get_conn(readonly=True)
    row = conn.execute("SELECT MAX(timestamp) FROM classifications").fetchone()
    return row[0] if row else None


def get_species_detail(name, limit=200):
    """Return full species detail for /api/species/{name}.

    Returns {common_name, scientific_name, count, detections: [{file, timestamp,
    confidence, raw_score, top3, birds}]} or None if species not found.
    """
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT * FROM classifications WHERE common_name=? AND action='classified' "
        "ORDER BY timestamp DESC LIMIT ?",
        (name, limit)
    ).fetchall()
    if not rows:
        return None

    detections = []
    for row in rows:
        e = _row_to_entry(row)
        detections.append({
            "file": e["file"],
            "timestamp": e.get("source_timestamp") or e["timestamp"],
            "confidence": e.get("best_detection", {}).get("confidence", 0),
            "raw_score": e.get("top_prediction", {}).get("raw_score", 0),
            "top3": e.get("top3", []),
            "birds": e.get("birds", []),
        })

    return {
        "common_name": name,
        "scientific_name": (
            detections[0].get("top3", [{}])[0].get("scientific_name", "")
            if detections else ""
        ),
        "count": len(detections),
        "detections": detections,
    }


def get_entries_by_files(filenames):
    """Batch lookup: returns {filename: entry_dict} for given filenames.

    SQLite limits ~999 variables per query, so we batch at 500.
    """
    if not filenames:
        return {}
    conn = get_conn(readonly=True)
    result = {}
    fnames = list(filenames)
    batch_size = 500
    for i in range(0, len(fnames), batch_size):
        batch = fnames[i:i + batch_size]
        placeholders = ",".join("?" * len(batch))
        rows = conn.execute(
            f"SELECT * FROM classifications WHERE file IN ({placeholders})",
            batch
        ).fetchall()
        for row in rows:
            result[row["file"]] = _row_to_entry(row)
    return result


def get_classified_for_pending():
    """Return all classified entries pre-shaped for the review/pending endpoint.

    Returns list of dicts with: file, source_timestamp, timestamp, species,
    confidence, raw_score, top3, raw_top3, birds.
    Only parses the JSON fields actually needed — lighter than full _row_to_entry().
    """
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT file, source_timestamp, timestamp, common_name, "
        "best_detection_json, top_prediction_json, top3_json, raw_top3_json, birds_json "
        "FROM classifications WHERE action='classified' ORDER BY timestamp DESC"
    ).fetchall()
    results = []
    for r in rows:
        tp = json.loads(r["top_prediction_json"]) if r["top_prediction_json"] else {}
        bd = json.loads(r["best_detection_json"]) if r["best_detection_json"] else {}
        birds = json.loads(r["birds_json"]) if r["birds_json"] else []
        results.append({
            "file": r["file"],
            "source_timestamp": r["source_timestamp"],
            "timestamp": r["timestamp"],
            "species": normalize_species(tp.get("common_name", "unknown")),
            "confidence": bd.get("confidence", 0),
            "raw_score": tp.get("raw_score", 0),
            "top3": json.loads(r["top3_json"]) if r["top3_json"] else [],
            "raw_top3": json.loads(r["raw_top3_json"]) if r["raw_top3_json"] else [],
            "birds": birds,
        })
    return results


def get_species_timestamps(species_name):
    """Return [{timestamp, camera}] for a species (for activity endpoint)."""
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT source_timestamp, camera FROM classifications "
        "WHERE common_name=? AND action='classified'",
        (species_name,)
    ).fetchall()
    return [{"timestamp": r["source_timestamp"], "camera": r["camera"]} for r in rows]


def get_all_classified_brief():
    """Return [(source_timestamp, common_name)] for all classified entries.

    Lightweight query for activity endpoints that don't need full entry data.
    """
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT source_timestamp, common_name FROM classifications "
        "WHERE action='classified' AND common_name IS NOT NULL"
    ).fetchall()
    return [(r["source_timestamp"], r["common_name"]) for r in rows]


def get_classified_since_brief(cutoff_date, species=None):
    """Return [(source_timestamp, common_name)] since cutoff_date.

    Optional species filter. For activity/heatmap endpoint.
    """
    conn = get_conn(readonly=True)
    if species and species != "all":
        rows = conn.execute(
            "SELECT source_timestamp, common_name FROM classifications "
            "WHERE action='classified' AND source_date >= ? AND common_name=?",
            (cutoff_date, normalize_species(species))
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT source_timestamp, common_name FROM classifications "
            "WHERE action='classified' AND source_date >= ? AND common_name IS NOT NULL",
            (cutoff_date,)
        ).fetchall()
    return [(r["source_timestamp"], r["common_name"]) for r in rows]


def get_species_counts_for_activity():
    """Return [{name, count}] for all classified species.

    For /api/activity/species-list endpoint (camera detections only).
    """
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT common_name, COUNT(*) as count FROM classifications "
        "WHERE action='classified' AND common_name IS NOT NULL "
        "GROUP BY common_name ORDER BY count DESC"
    ).fetchall()
    return [{"name": normalize_species(r["common_name"]), "count": r["count"]} for r in rows]
