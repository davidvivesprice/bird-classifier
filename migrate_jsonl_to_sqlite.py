#!/usr/bin/env python3
"""
Migrate classifications.jsonl → classifications.db (SQLite).

One-shot script: reads the entire JSONL, creates a fresh SQLite DB,
inserts all entries with proper indexes.  Safe to re-run — it drops
and recreates the table each time.

Usage:
    python3 migrate_jsonl_to_sqlite.py [--jsonl PATH] [--db PATH] [--batch-size N]

Defaults:
    --jsonl  ~/bird-snapshots/logs/classifications.jsonl
    --db     ~/bird-snapshots/logs/classifications.db
    --batch-size 5000
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# --- Schema ---

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
]

INSERT_SQL = """
INSERT OR REPLACE INTO classifications (
    file, camera, timestamp, source_timestamp, source_date, action,
    detect_ms, classify_ms, total_ms, detections,
    best_detection_json, top_prediction_json, top3_json, raw_top3_json, birds_json,
    common_name, scientific_name, raw_score, confidence,
    range_filter_applied, original_species, filter_reason, extra_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Subspecies / regional forms → canonical parent species
# Must match api.py's SPECIES_ALIASES
SPECIES_ALIASES = {
    "Slate-colored Junco": "Dark-eyed Junco",
    "Myrtle Warbler": "Yellow-rumped Warbler",
    "Feral Pigeon": "Rock Pigeon",
}

# Known top-level fields — anything else goes into extra_json
KNOWN_FIELDS = {
    "file", "camera", "timestamp", "source_timestamp", "action",
    "detect_ms", "classify_ms", "total_ms", "detections",
    "best_detection", "top_prediction", "top3", "raw_top3", "birds",
    "common_name", "scientific_name", "raw_score", "confidence",
    "range_filter_applied", "original_species", "filter_reason", "filter_flags",
}


def normalize_species(name):
    return SPECIES_ALIASES.get(name, name)


def entry_to_row(e):
    """Convert a JSONL dict to a SQLite row tuple."""
    # Normalize species names
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

    # Collect unknown fields into extra_json
    extra = {k: v for k, v in e.items() if k not in KNOWN_FIELDS}
    extra_json = json.dumps(extra) if extra else None

    return (
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


def migrate(jsonl_path, db_path, batch_size=5000):
    if not jsonl_path.exists():
        print(f"ERROR: JSONL file not found: {jsonl_path}")
        sys.exit(1)

    file_size_mb = jsonl_path.stat().st_size / (1024 * 1024)
    print(f"Source: {jsonl_path} ({file_size_mb:.1f} MB)")
    print(f"Target: {db_path}")

    # Remove existing DB for clean migration
    if db_path.exists():
        backup = db_path.with_suffix(".db.bak")
        print(f"Backing up existing DB to {backup}")
        os.replace(db_path, backup)

    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")

    # Create table without indexes (faster bulk insert)
    conn.execute("DROP TABLE IF EXISTS classifications")
    conn.execute(CREATE_TABLE)
    conn.commit()

    print("Importing entries...")
    t0 = time.time()
    total = 0
    skipped = 0
    corrupt = 0
    dupes = 0
    seen_files = set()
    batch = []

    with open(jsonl_path, "rb") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                corrupt += 1
                continue

            fname = e.get("file", "")
            if not fname:
                skipped += 1
                continue

            # Deduplicate: keep last occurrence (same as api.py behavior)
            if fname in seen_files:
                dupes += 1
            seen_files.add(fname)

            try:
                row = entry_to_row(e)
                batch.append(row)
            except Exception as exc:
                print(f"  WARN line {line_num}: {exc}")
                skipped += 1
                continue

            if len(batch) >= batch_size:
                conn.executemany(INSERT_SQL, batch)
                conn.commit()
                total += len(batch)
                elapsed = time.time() - t0
                rate = total / elapsed if elapsed > 0 else 0
                print(f"  {total:,} entries ({rate:,.0f}/s)...", end="\r")
                batch = []

    # Flush remaining
    if batch:
        conn.executemany(INSERT_SQL, batch)
        conn.commit()
        total += len(batch)

    elapsed = time.time() - t0
    print(f"\nImported {total:,} entries in {elapsed:.1f}s ({total/elapsed:,.0f}/s)")
    print(f"  Corrupt lines: {corrupt}")
    print(f"  Skipped (no file): {skipped}")
    print(f"  Duplicates (last wins): {dupes}")

    # Now create indexes
    print("Creating indexes...")
    t1 = time.time()
    for idx_sql in INDEXES:
        conn.execute(idx_sql)
    conn.commit()
    print(f"Indexes created in {time.time() - t1:.1f}s")

    # Verify
    row_count = conn.execute("SELECT COUNT(*) FROM classifications").fetchone()[0]
    classified = conn.execute("SELECT COUNT(*) FROM classifications WHERE action='classified'").fetchone()[0]
    species = conn.execute("SELECT COUNT(DISTINCT common_name) FROM classifications WHERE common_name IS NOT NULL").fetchone()[0]
    dates = conn.execute("SELECT COUNT(DISTINCT source_date) FROM classifications WHERE source_date IS NOT NULL").fetchone()[0]

    db_size_mb = db_path.stat().st_size / (1024 * 1024)

    print(f"\n--- Verification ---")
    print(f"Total rows:     {row_count:,}")
    print(f"Classified:     {classified:,}")
    print(f"Species:        {species}")
    print(f"Dates:          {dates}")
    print(f"DB size:        {db_size_mb:.1f} MB")
    print(f"JSONL size:     {file_size_mb:.1f} MB")
    print(f"Compression:    {db_size_mb/file_size_mb*100:.0f}%")

    conn.close()
    print("\nDone! Migration complete.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate classifications JSONL to SQLite")
    parser.add_argument("--jsonl", type=Path,
                        default=Path.home() / "bird-snapshots" / "logs" / "classifications.jsonl")
    parser.add_argument("--db", type=Path,
                        default=Path.home() / "bird-snapshots" / "logs" / "classifications.db")
    parser.add_argument("--batch-size", type=int, default=5000)
    args = parser.parse_args()
    migrate(args.jsonl, args.db, args.batch_size)
