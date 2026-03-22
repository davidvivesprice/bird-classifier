#!/usr/bin/env python3
"""Migrate reviews.jsonl to SQLite reviews table.

Safe to re-run (INSERT OR REPLACE). Later entries for same file win.
"""
import json
import sqlite3
import sys
from pathlib import Path

JSONL_PATH = Path(__file__).parent / "dashboard" / "reviews.jsonl"
DB_PATH = Path.home() / "bird-snapshots" / "logs" / "classifications.db"

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS reviews (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file             TEXT    UNIQUE NOT NULL,
    verdict          TEXT    NOT NULL,
    correct_species  TEXT    DEFAULT '',
    bird_index       INTEGER DEFAULT 0,
    missed_birds     INTEGER DEFAULT 0,
    timestamp        TEXT    NOT NULL,
    reviewer         TEXT    DEFAULT 'dashboard'
);
"""

CREATE_INDEXES_SQL = [
    "CREATE INDEX IF NOT EXISTS idx_reviews_file    ON reviews(file);",
    "CREATE INDEX IF NOT EXISTS idx_reviews_verdict ON reviews(verdict);",
    "CREATE INDEX IF NOT EXISTS idx_reviews_species ON reviews(correct_species);",
]

INSERT_SQL = """
INSERT OR REPLACE INTO reviews
    (file, verdict, correct_species, bird_index, missed_birds, timestamp, reviewer)
VALUES
    (:file, :verdict, :correct_species, :bird_index, :missed_birds, :timestamp, :reviewer)
"""


def coerce_bool(value) -> int:
    """Convert boolean or boolean-string to INTEGER 0/1."""
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        return 1 if value.lower() == "true" else 0
    # Already an int or int-like
    return int(value)


def main():
    if not JSONL_PATH.exists():
        print(f"ERROR: JSONL file not found: {JSONL_PATH}", file=sys.stderr)
        sys.exit(1)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(CREATE_TABLE_SQL)
        for idx_sql in CREATE_INDEXES_SQL:
            conn.execute(idx_sql)
        conn.commit()

        inserted = 0
        skipped_empty = 0
        skipped_malformed = 0

        with open(JSONL_PATH, "r", encoding="utf-8") as fh:
            for lineno, raw_line in enumerate(fh, start=1):
                line = raw_line.strip()
                if not line:
                    skipped_empty += 1
                    continue

                try:
                    entry = json.loads(line)
                except json.JSONDecodeError as exc:
                    print(
                        f"WARNING: line {lineno} — malformed JSON, skipping: {exc}",
                        file=sys.stderr,
                    )
                    skipped_malformed += 1
                    continue

                # Required fields
                file_val = entry.get("file", "").strip()
                verdict_val = entry.get("verdict", "").strip()
                timestamp_val = entry.get("timestamp", "").strip()

                if not file_val or not verdict_val or not timestamp_val:
                    print(
                        f"WARNING: line {lineno} — missing required field "
                        f"(file/verdict/timestamp), skipping: {line[:80]}",
                        file=sys.stderr,
                    )
                    skipped_malformed += 1
                    continue

                # Optional fields with defaults
                correct_species = entry.get("correct_species", "")
                bird_index = int(entry.get("bird_index", 0))
                missed_birds = coerce_bool(entry.get("missed_birds", 0))

                conn.execute(
                    INSERT_SQL,
                    {
                        "file": file_val,
                        "verdict": verdict_val,
                        "correct_species": correct_species,
                        "bird_index": bird_index,
                        "missed_birds": missed_birds,
                        "timestamp": timestamp_val,
                        "reviewer": "dashboard",
                    },
                )
                inserted += 1

        conn.commit()

        # Summary
        total = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
        verdicts = conn.execute(
            "SELECT verdict, COUNT(*) FROM reviews GROUP BY verdict ORDER BY COUNT(*) DESC"
        ).fetchall()

        print(f"Migration complete.")
        print(f"  Lines processed : {inserted + skipped_empty + skipped_malformed}")
        print(f"  Rows upserted   : {inserted}")
        print(f"  Empty lines     : {skipped_empty}")
        print(f"  Malformed lines : {skipped_malformed}")
        print(f"  Total in table  : {total}")
        print(f"  Verdict breakdown:")
        for verdict, count in verdicts:
            print(f"    {verdict}: {count}")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
