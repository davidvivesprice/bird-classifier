#!/usr/bin/env python3
"""Verify every classifications.db row has a JPG on disk, and vice-versa.

Plan: docs/superpowers/plans/2026-04-22-data-integrity-audit.md

The orphan-row bug (David noted "this has bit us more than a few times")
needs both cleanup AND instrumentation — cleanup fixes the immediate
symptom; the log captures which rows + when so the next occurrence is
traceable to a pipeline state, not just silently repaired.

Usage:
    python3 tools/audit_data_integrity.py                 # report only (dry-run)
    python3 tools/audit_data_integrity.py --cull          # delete orphan DB rows
    python3 tools/audit_data_integrity.py --json          # machine-readable summary
"""
import argparse
import datetime
import json
import logging
import sqlite3
import sys
from pathlib import Path

SNAPSHOTS = Path.home() / "bird-snapshots"
DB = SNAPSHOTS / "logs" / "classifications.db"
CLASSIFIED = SNAPSHOTS / "classified"
ANNOTATED = SNAPSHOTS / "annotated"
PENDING = SNAPSHOTS / "pending"
TRASH = SNAPSHOTS / "trash"
CULLED = SNAPSHOTS / "culled"

# Search order matters for reporting "where did we find the jpg":
# classified/<species> is the preferred home; trash/culled are fine but
# a row claiming action='classified' pointing at a file in trash/ is a
# mismatch worth logging (not an orphan, but a consistency bug).
SEARCH_ROOTS = [
    ("classified", CLASSIFIED),
    ("annotated", ANNOTATED),
    ("pending", PENDING),
    ("trash", TRASH),
    ("culled", CULLED),
]

LOG_FILE = Path(__file__).parent / "audit_data_integrity.log"


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("audit_data_integrity")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
    return log


def build_disk_index() -> dict:
    """Return {filename: (root_label, path)} across all known snapshot roots.

    First-found-wins per the SEARCH_ROOTS order. We record the root so the
    audit can flag "row says classified but jpg is in trash" consistency bugs.
    """
    index: dict[str, tuple[str, Path]] = {}
    for label, root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.jpg"):
            index.setdefault(p.name, (label, p))
    return index


# Actions where a DB row is EXPECTED to have no JPG on disk:
# - 'no_bird' / 'skipped:no_bird' / 'skipped:background': detection-stage
#   logs; no file was ever saved.
# - 'trashed:*': file was moved to trash then eventually deleted; row stays
#   for history/review metrics.
# - 'culled_hallucination': quarantined by tools/cull_hallucination_window.py.
# Only rows with an action that implies "there SHOULD be a file" count as
# true orphans. That filter is what separates real data-integrity bugs
# from 100k+ by-design DB-only rows.
BY_DESIGN_NO_FILE_ACTIONS = {
    "no_bird",
    "skipped:no_bird",
    "skipped:background",
    "trashed:infrared",
    "trashed:review",
    "trashed:duplicate",
    "culled_hallucination",
}


def audit(conn: sqlite3.Connection) -> dict:
    disk = build_disk_index()
    cur = conn.execute(
        """
        SELECT id, file, common_name, action, source_timestamp
        FROM classifications
        """
    )
    rows = cur.fetchall()

    db_files: set[str] = {r["file"] for r in rows}
    orphan_rows = []            # row expects a file, none on disk (TRUE orphan)
    by_design_orphans = 0       # row is a trashed/no_bird record, file absent (expected)
    location_mismatches = []    # row is "classified" but jpg lives in trash/culled

    for r in rows:
        fname = r["file"]
        hit = disk.get(fname)
        if hit is None:
            if r["action"] in BY_DESIGN_NO_FILE_ACTIONS:
                by_design_orphans += 1
                continue
            orphan_rows.append({
                "id": r["id"],
                "file": fname,
                "species": r["common_name"],
                "source_timestamp": r["source_timestamp"],
                "action": r["action"],
            })
            continue
        label, _path = hit
        if r["action"] == "classified" and label in ("trash", "culled"):
            location_mismatches.append({
                "id": r["id"],
                "file": fname,
                "species": r["common_name"],
                "source_timestamp": r["source_timestamp"],
                "found_in": label,
            })

    # Orphan files: JPG under classified/ with no matching DB row.
    orphan_files: list[str] = []
    for fname, (label, path) in disk.items():
        if fname in db_files:
            continue
        if label == "classified":
            orphan_files.append(str(path))

    return {
        "total_rows_checked": len(rows),
        "disk_files_found": len(disk),
        "orphan_rows": orphan_rows,
        "by_design_orphans": by_design_orphans,
        "orphan_files_count": len(orphan_files),
        "orphan_files_sample": orphan_files[:20],
        "location_mismatches": location_mismatches,
    }


def cull_orphan_rows(conn: sqlite3.Connection, orphans: list) -> int:
    if not orphans:
        return 0
    ids = [o["id"] for o in orphans]
    placeholders = ",".join("?" * len(ids))
    cur = conn.execute(
        f"DELETE FROM classifications WHERE id IN ({placeholders})", ids,
    )
    conn.commit()
    return cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cull", action="store_true",
                    help="delete orphan DB rows (default: report only)")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON summary to stdout")
    args = ap.parse_args()

    log = _setup_logger()

    if not DB.exists():
        print(f"ERROR: classifications.db not found at {DB}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    report = audit(conn)

    log.info(
        "audit: rows=%d disk=%d orphan_rows=%d by_design=%d orphan_files=%d mismatches=%d",
        report["total_rows_checked"], report["disk_files_found"],
        len(report["orphan_rows"]), report["by_design_orphans"],
        report["orphan_files_count"], len(report["location_mismatches"]),
    )
    for o in report["orphan_rows"]:
        log.info("orphan_row id=%d file=%s species=%s ts=%s action=%s",
                 o["id"], o["file"], o["species"],
                 o["source_timestamp"], o["action"])
    for m in report["location_mismatches"][:100]:
        log.info("location_mismatch id=%d file=%s species=%s found_in=%s",
                 m["id"], m["file"], m["species"], m["found_in"])

    culled = 0
    if args.cull and report["orphan_rows"]:
        culled = cull_orphan_rows(conn, report["orphan_rows"])
        log.warning("culled %d orphan rows", culled)
    report["culled"] = culled
    conn.close()

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(f"rows checked:          {report['total_rows_checked']}")
        print(f"disk files found:      {report['disk_files_found']}")
        print(f"orphan rows:           {len(report['orphan_rows'])}  (row expects a file, none on disk)")
        print(f"  by-design:           {report['by_design_orphans']}  (no_bird/trashed/skipped rows where absence is expected)")
        print(f"orphan files:          {report['orphan_files_count']}  (not deleted; sample below)")
        print(f"location mismatches:   {len(report['location_mismatches'])}  (row says classified, file is in trash/culled)")
        for f in report["orphan_files_sample"]:
            print(f"  file orphan: {f}")
        for m in report["location_mismatches"][:10]:
            print(f"  mismatch: id={m['id']} file={m['file']} found_in={m['found_in']}")
        if args.cull:
            print(f"\nCULLED: {culled} DB rows")
        else:
            print("\n(dry-run; pass --cull to delete orphan rows)")

    return 0 if not report["orphan_rows"] else 1


if __name__ == "__main__":
    sys.exit(main())
