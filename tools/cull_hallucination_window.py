#!/usr/bin/env python3
"""Cull the post-2026-04-19 hallucination window from classifications.db.

The Sonoma-migration hi-res re-crop path introduced a 2-5 second window
between detection and the 1080p frame grab. During that window the bird
leaves the bbox, so AIY classifies empty feeder background as (e.g.)
"American Robin." Rows written by this code path are garbage.

This script identifies affected rows, moves their JPGs to a dated
quarantine directory at ~/bird-snapshots/culled/<date>/, and marks the
DB rows with action='culled_hallucination'. Reviewed rows are preserved.

Plan: docs/superpowers/plans/2026-04-22-garbage-data-cull.md

Usage:
    python3 tools/cull_hallucination_window.py                 # dry-run (default)
    python3 tools/cull_hallucination_window.py --execute       # actually do it
    python3 tools/cull_hallucination_window.py --cutoff T      # override cutoff
    python3 tools/cull_hallucination_window.py --json          # machine-readable
"""
import argparse
import datetime
import json
import logging
import shutil
import sqlite3
import sys
from pathlib import Path

SNAPSHOTS = Path.home() / "bird-snapshots"
DB = SNAPSHOTS / "logs" / "classifications.db"
CLASSIFIED = SNAPSHOTS / "classified"
ANNOTATED = SNAPSHOTS / "annotated"
PENDING = SNAPSHOTS / "pending"
CULL_ROOT = SNAPSHOTS / "culled" / datetime.date.today().isoformat()

DEFAULT_CUTOFF = "2026-04-19T21:16"

LOG_FILE = Path(__file__).parent / "cull_hallucination_window.log"


def find_jpg(file: str) -> Path | None:
    """Search the known snapshot dirs for the JPG. Returns first hit or None."""
    # classified/<species>/<file>
    if CLASSIFIED.exists():
        for species_dir in CLASSIFIED.glob("*"):
            if not species_dir.is_dir():
                continue
            p = species_dir / file
            if p.exists():
                return p
    # annotated/<file>
    p = ANNOTATED / file
    if p.exists():
        return p
    # pending/<file>
    p = PENDING / file
    if p.exists():
        return p
    return None


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("cull_hallucination_window")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fh = logging.FileHandler(LOG_FILE)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        log.addHandler(fh)
    return log


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--execute", action="store_true",
                    help="actually move files + update DB; default is dry-run")
    ap.add_argument("--cutoff", default=DEFAULT_CUTOFF,
                    help=f"ISO source_timestamp; rows >= this are candidates "
                         f"(default {DEFAULT_CUTOFF!r})")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON summary to stdout")
    args = ap.parse_args()

    log = _setup_logger()

    if not DB.exists():
        print(f"ERROR: classifications.db not found at {DB}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row

    # Rows in window AND not reviewed AND not already culled. Reviews live in a
    # separate table — LEFT JOIN then filter to rows without a matching review.
    cur = conn.execute(
        """
        SELECT c.id, c.file, c.common_name, c.source_timestamp, c.action
        FROM classifications c
        LEFT JOIN reviews r ON r.file = c.file
        WHERE c.source_timestamp >= ?
          AND r.file IS NULL
          AND c.action != 'culled_hallucination'
        ORDER BY c.source_timestamp
        """,
        (args.cutoff,),
    )
    rows = cur.fetchall()
    scope_msg = f"scope: {len(rows)} rows >= {args.cutoff}, unreviewed, not-already-culled"
    log.info(scope_msg)

    if args.execute:
        CULL_ROOT.mkdir(parents=True, exist_ok=True)

    moved = 0
    missing = 0  # pre-existing orphans (no JPG on disk)
    errors = 0

    for row in rows:
        jpg = find_jpg(row["file"])
        if jpg is None:
            missing += 1
            log.info("pre-orphan: id=%d file=%s species=%s ts=%s",
                     row["id"], row["file"], row["common_name"],
                     row["source_timestamp"])
            if args.execute:
                try:
                    conn.execute(
                        "UPDATE classifications SET action='culled_hallucination' WHERE id=?",
                        (row["id"],),
                    )
                except Exception as e:
                    errors += 1
                    log.exception("error marking orphan id=%d: %s", row["id"], e)
            continue

        # Also move annotated/<file> if present (mirror of classified).
        ann = ANNOTATED / row["file"]
        sources = [jpg] + ([ann] if ann.exists() else [])

        if args.execute:
            try:
                for src in sources:
                    dst = CULL_ROOT / src.name
                    # Collision avoidance when classified + annotated share a basename
                    if dst.exists():
                        dst = CULL_ROOT / f"{src.parent.name}__{src.name}"
                    shutil.move(str(src), str(dst))
                conn.execute(
                    "UPDATE classifications SET action='culled_hallucination' WHERE id=?",
                    (row["id"],),
                )
                moved += 1
                log.info("culled: id=%d file=%s species=%s",
                         row["id"], row["file"], row["common_name"])
            except Exception as e:
                errors += 1
                log.exception("error culling id=%d: %s", row["id"], e)
        else:
            moved += 1  # count what WOULD be culled

    if args.execute:
        conn.commit()
    conn.close()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    summary = {
        "mode": mode,
        "cutoff": args.cutoff,
        "scope_rows": len(rows),
        "would_cull_or_culled": moved,
        "pre_orphan_rows": missing,
        "errors": errors,
        "log": str(LOG_FILE),
        "quarantine_dir": str(CULL_ROOT),
    }
    log.info("summary: %s", summary)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"\n[{mode}]")
        print(f"  cutoff:                {args.cutoff}")
        print(f"  scope rows:            {len(rows)}")
        print(f"  would-cull / culled:   {moved}")
        print(f"  pre-orphan rows:       {missing}")
        print(f"  errors:                {errors}")
        print(f"  log:                   {LOG_FILE}")
        print(f"  quarantine dir:        {CULL_ROOT}")
        if not args.execute:
            print("\n  (dry-run; pass --execute to actually cull)")

    return 0 if errors == 0 else 3


if __name__ == "__main__":
    sys.exit(main())
