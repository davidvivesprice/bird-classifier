#!/usr/bin/env python3
"""Populate visits table from existing classifications.

Groups consecutive same-species detections into visits using a 60-second
gap threshold. Idempotent — clears and rebuilds visits table on each run.

Usage:
    python populate_visits.py [--gap SECONDS] [--date YYYY-MM-DD]
"""
import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
DEFAULT_GAP = 60  # seconds

CREATE_VISITS_SQL = """
CREATE TABLE IF NOT EXISTS visits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    camera          TEXT    NOT NULL,
    species         TEXT    NOT NULL,
    scientific_name TEXT,
    start_time      TEXT    NOT NULL,
    end_time        TEXT    NOT NULL,
    duration_sec    REAL    NOT NULL,
    frame_count     INTEGER NOT NULL DEFAULT 1,
    best_confidence REAL,
    avg_confidence  REAL,
    best_file       TEXT,
    source_date     TEXT
)
"""


def parse_ts(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")


def main():
    parser = argparse.ArgumentParser(description="Populate visits table from classifications.")
    parser.add_argument("--gap", type=int, default=DEFAULT_GAP,
                        help=f"Gap threshold in seconds (default: {DEFAULT_GAP})")
    parser.add_argument("--date", type=str, default=None,
                        help="Only process a specific date (YYYY-MM-DD)")
    args = parser.parse_args()

    gap_seconds = args.gap
    filter_date = args.date

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    # Create visits table if it doesn't exist
    conn.execute(CREATE_VISITS_SQL)
    conn.commit()

    # Clear existing visits (idempotent)
    if filter_date:
        conn.execute("DELETE FROM visits WHERE source_date = ?", (filter_date,))
    else:
        conn.execute("DELETE FROM visits")
    conn.commit()

    print("Populating visits from classifications...")
    print(f"  Gap threshold: {gap_seconds} seconds")
    if filter_date:
        print(f"  Filtering to date: {filter_date}")

    # Build query
    where_clause = "WHERE action = 'classified' AND common_name IS NOT NULL"
    params = []
    if filter_date:
        where_clause += " AND source_date = ?"
        params.append(filter_date)

    query = f"""
        SELECT file, camera, common_name, scientific_name,
               source_timestamp, source_date, confidence, raw_score
        FROM classifications
        {where_clause}
        ORDER BY camera, common_name, source_timestamp
    """

    rows = conn.execute(query, params).fetchall()
    total_detections = len(rows)
    print(f"  Processing {total_detections:,} classified detections...\n")

    if total_detections == 0:
        print("No detections found. Exiting.")
        conn.close()
        return

    # active_visits: (camera, species) -> visit state dict
    active_visits = {}

    visits_to_insert = []

    def close_visit(key):
        """Finalize and queue a visit for insertion."""
        v = active_visits.pop(key)
        duration = (v["end_ts"] - v["start_ts"]).total_seconds()
        visits_to_insert.append({
            "camera": v["camera"],
            "species": v["species"],
            "scientific_name": v["scientific_name"],
            "start_time": v["start_ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "end_time": v["end_ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "duration_sec": duration,
            "frame_count": v["frame_count"],
            "best_confidence": v["best_confidence"],
            "avg_confidence": v["sum_confidence"] / v["frame_count"],
            "best_file": v["best_file"],
            "source_date": v["source_date"],
        })

    # Track per-species stats for summary
    species_stats = {}  # species -> {"detections": int, "visits": int}

    for row in rows:
        camera = row["camera"]
        species = row["common_name"]
        sci_name = row["scientific_name"]
        ts_str = row["source_timestamp"]
        src_date = row["source_date"]
        confidence = row["confidence"] or 0.0
        filename = row["file"]

        if not ts_str:
            continue

        try:
            ts = parse_ts(ts_str)
        except ValueError:
            continue

        key = (camera, species)

        # Update per-species detection count
        if species not in species_stats:
            species_stats[species] = {"detections": 0, "visits": 0}
        species_stats[species]["detections"] += 1

        if key in active_visits:
            v = active_visits[key]
            gap = (ts - v["end_ts"]).total_seconds()

            if gap <= gap_seconds:
                # Extend current visit
                v["end_ts"] = ts
                v["frame_count"] += 1
                v["sum_confidence"] += confidence
                if confidence > v["best_confidence"]:
                    v["best_confidence"] = confidence
                    v["best_file"] = filename
                # Keep source_date as the start date of the visit
            else:
                # Gap too large — close old visit, start new one
                close_visit(key)
                species_stats[species]["visits"] += 1
                active_visits[key] = {
                    "camera": camera,
                    "species": species,
                    "scientific_name": sci_name,
                    "start_ts": ts,
                    "end_ts": ts,
                    "frame_count": 1,
                    "best_confidence": confidence,
                    "sum_confidence": confidence,
                    "best_file": filename,
                    "source_date": src_date,
                }
        else:
            # Start a new visit
            active_visits[key] = {
                "camera": camera,
                "species": species,
                "scientific_name": sci_name,
                "start_ts": ts,
                "end_ts": ts,
                "frame_count": 1,
                "best_confidence": confidence,
                "sum_confidence": confidence,
                "best_file": filename,
                "source_date": src_date,
            }

    # Close all remaining active visits
    for key in list(active_visits.keys()):
        species = key[1]
        close_visit(key)
        species_stats[species]["visits"] += 1

    # Batch insert all visits
    conn.executemany("""
        INSERT INTO visits
            (camera, species, scientific_name, start_time, end_time,
             duration_sec, frame_count, best_confidence, avg_confidence,
             best_file, source_date)
        VALUES
            (:camera, :species, :scientific_name, :start_time, :end_time,
             :duration_sec, :frame_count, :best_confidence, :avg_confidence,
             :best_file, :source_date)
    """, visits_to_insert)
    conn.commit()

    # Print per-species summary (top 15 by detection count)
    sorted_species = sorted(species_stats.items(), key=lambda x: x[1]["detections"], reverse=True)
    for sp, stats in sorted_species[:15]:
        d = stats["detections"]
        v = stats["visits"]
        ratio = d / v if v else float("inf")
        print(f"  {sp}: {d:,} detections → {v:,} visits ({ratio:.1f}x compression)")

    total_visits = len(visits_to_insert)
    total_species = len(species_stats)
    overall_ratio = total_detections / total_visits if total_visits else 0

    print(f"""
Summary:
  Total detections: {total_detections:,}
  Total visits:     {total_visits:,}
  Compression ratio: {overall_ratio:.1f}x
  Species: {total_species}
""")

    conn.close()


if __name__ == "__main__":
    main()
