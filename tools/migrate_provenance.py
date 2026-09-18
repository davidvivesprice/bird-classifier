#!/usr/bin/env python3
"""Backfill the provenance columns of `classifications` from extra_json.

Post-mortem must-fix 1 + 3: label_source, lock_species, auth_species,
auth_confidence, disagreement, track_id, pts, model_source come from the
row's extra_json; era / crop_valid come from the Pi snapshot-pipeline
cutoff (source_timestamp < 2026-05-12 → the stored bbox does not describe
the stored image). image_w / image_h are only known by opening the JPEG,
so they are filled on request (--image-dims) from the file's header.

Idempotent and resumable: rows are selected WHERE era IS NULL and written in
committed batches, so a killed run simply continues on the next invocation.
--force re-derives every row but keeps image_w/image_h from an earlier
--image-dims pass (and the crop_valid=0 they justified). --dry-run opens the
DB read-only and prints the counts it would write.

Never run this against the live DB while the pipeline writes to it. The
default guard refuses ~/bird-snapshots/logs/classifications.db unless --live
is given; work on a copy:

    sqlite3 -readonly ~/bird-snapshots/logs/classifications.db \
        ".backup ~/staging-provenance/classifications-copy.db"
    python3 tools/migrate_provenance.py --db ~/staging-provenance/classifications-copy.db --dry-run
    python3 tools/migrate_provenance.py --db ~/staging-provenance/classifications-copy.db --image-dims
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import classifications_db as cdb  # noqa: E402
from pipeline.snapshot_writer import (  # noqa: E402
    SNAPSHOT_ERA, SNAPSHOT_ROOT, _crop_valid, _safe_species_dir,
)

LIVE_DB_PATH = Path.home() / "bird-snapshots" / "logs" / "classifications.db"
CROP_VALID_CUTOFF = "2026-05-12"       # Pi: first day the HLS-by-PTS snapshot path was live
PRE_ERA = "pre-hls-pts"
BBOX_SPACE = "image"

ACCEPTANCE_SQL = (
    "SELECT count(*) FROM classifications WHERE action='classified' "
    "AND (label_source IS NULL OR track_id IS NULL OR pts IS NULL)"
)

_SELECT_COLS = ("id", "file", "common_name", "timestamp", "source_timestamp",
                "best_detection_json", "extra_json")
# Carried over from a previous --image-dims run when this run does not
# recompute them, so a bare --force never NULLs filled dims.
_CARRY_COLS = ("image_w", "image_h")


def _jpeg_size(path: Path):
    try:
        from PIL import Image
        with Image.open(path) as im:
            return int(im.size[0]), int(im.size[1])
    except Exception:
        return None


def _bbox_of(best_detection_json):
    try:
        box = json.loads(best_detection_json).get("box") if best_detection_json else None
    except (json.JSONDecodeError, AttributeError, TypeError):
        return None
    return box if isinstance(box, list) and len(box) == 4 else None


def derive_row(row: dict, cutoff: str, image_dims: bool, snapshot_root: Path) -> dict:
    """All provenance column values for one classifications row."""
    try:
        extra = json.loads(row["extra_json"]) if row["extra_json"] else {}
    except json.JSONDecodeError:
        extra = {}
    if not isinstance(extra, dict):
        extra = {}
    values = cdb.derive_provenance(extra, common_name=row["common_name"])

    ts = row["source_timestamp"] or row["timestamp"] or ""
    pre = bool(ts) and ts < cutoff
    box = _bbox_of(row["best_detection_json"])
    values["bbox_space"] = BBOX_SPACE if box else None
    values["era"] = PRE_ERA if pre else SNAPSHOT_ERA
    crop_valid = 0 if pre else 1

    image_w, image_h = row.get("image_w"), row.get("image_h")
    if image_dims and row["file"]:
        candidate = snapshot_root / _safe_species_dir(row["common_name"]) / row["file"]
        size = _jpeg_size(candidate) if candidate.exists() else None
        if size:
            image_w, image_h = size
    # Dims known (this run or an earlier one) and the box misses the image:
    # the crop cannot describe the stored file.
    if box and image_w and image_h and not _crop_valid(box, image_w, image_h):
        crop_valid = 0
    values["image_w"] = image_w
    values["image_h"] = image_h
    values["crop_valid"] = crop_valid
    return values


def _pending_sql(force: bool, carry: bool = True) -> str:
    cols = _SELECT_COLS + (_CARRY_COLS if carry else ())
    where = "id > ?" if force else "id > ? AND era IS NULL"
    return (f"SELECT {', '.join(cols)} FROM classifications "
            f"WHERE {where} ORDER BY id LIMIT ?")


def _print_summary(conn, tallies: Counter, era_tally: Counter, n_rows: int, n_dims: int,
                   elapsed: float, wrote: bool) -> None:
    mode = "written" if wrote else "would write (dry-run)"
    print(f"\n{n_rows} rows {mode} in {elapsed:.1f}s"
          + (f" ({n_rows / elapsed:.0f} rows/s)" if elapsed > 0 and n_rows else ""))
    print("\nlabel_source counts (this run):")
    for src, n in sorted(tallies.items(), key=lambda kv: -kv[1]):
        print(f"  {n:7d}  {src if src is not None else 'NULL'}")
    print("\nera × crop_valid (this run):")
    for (era, cv), n in sorted(era_tally.items(), key=lambda kv: -kv[1]):
        print(f"  {n:7d}  era={era} crop_valid={cv}")
    print(f"\nimage dims filled (this run): {n_dims}")
    if wrote:
        print("\nlabel_source counts (whole table):")
        for src, n in conn.execute(
                "SELECT label_source, count(*) FROM classifications GROUP BY 1 ORDER BY 2 DESC"):
            print(f"  {n:7d}  {src if src is not None else 'NULL'}")
        total = conn.execute(ACCEPTANCE_SQL).fetchone()[0]
        recent = conn.execute(ACCEPTANCE_SQL + " AND era = ?", (SNAPSHOT_ERA,)).fetchone()[0]
        unmigrated = conn.execute(
            "SELECT count(*) FROM classifications WHERE era IS NULL").fetchone()[0]
        print(f"\nacceptance (classified rows missing label_source/track_id/pts): "
              f"{total} overall, {recent} in era={SNAPSHOT_ERA}")
        print(f"rows still unmigrated (era IS NULL): {unmigrated}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", required=True, help="classifications.db to migrate (work on a copy)")
    ap.add_argument("--dry-run", action="store_true", help="read-only; print what would change")
    ap.add_argument("--force", action="store_true", help="re-derive rows already migrated")
    ap.add_argument("--batch-size", type=int, default=2000)
    ap.add_argument("--max-batches", type=int, default=0, help="stop after N batches (0 = all)")
    ap.add_argument("--image-dims", action="store_true",
                    help="fill image_w/image_h from the JPEG header under the snapshot root")
    ap.add_argument("--snapshot-root", default=None,
                    help=f"where <species>/<file>.jpg live (default {SNAPSHOT_ROOT})")
    ap.add_argument("--cutoff", default=CROP_VALID_CUTOFF,
                    help="source_timestamp below this → crop_valid=0, era=pre-hls-pts")
    ap.add_argument("--live", action="store_true",
                    help="allow writing to the live DB path (you almost never want this)")
    args = ap.parse_args(argv)

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(f"ERROR: {db_path} does not exist", file=sys.stderr)
        return 2
    is_live = db_path.resolve() == LIVE_DB_PATH.resolve()
    if is_live and not args.dry_run and not args.live:
        print(f"REFUSING: {db_path} is the live pipeline DB. Copy it first "
              f"(see module docstring) or pass --live.", file=sys.stderr)
        return 3
    snapshot_root = Path(args.snapshot_root).expanduser() if args.snapshot_root else SNAPSHOT_ROOT

    if args.dry_run:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row

    have = {r[1] for r in conn.execute("PRAGMA table_info(classifications)")}
    missing = [name for name in cdb.PROVENANCE_COLUMN_NAMES if name not in have]
    if args.dry_run:
        if missing:
            print(f"dry-run: would add columns {', '.join(missing)}")
        force = args.force or bool(missing)     # no era column yet → every row is pending
        select_sql = _pending_sql(force, carry=all(c in have for c in _CARRY_COLS))
    else:
        added = cdb.ensure_provenance_columns(conn)
        conn.commit()
        if added:
            print(f"added columns: {', '.join(added)}")
        select_sql = _pending_sql(args.force)

    update_sql = ("UPDATE classifications SET "
                  + ", ".join(f"{name} = ?" for name in cdb.PROVENANCE_COLUMN_NAMES)
                  + " WHERE id = ?")

    tallies: Counter = Counter()
    era_tally: Counter = Counter()
    n_rows = n_dims = n_batches = 0
    last_id = 0
    t0 = time.monotonic()
    while True:
        rows = conn.execute(select_sql, (last_id, args.batch_size)).fetchall()
        if not rows:
            break
        updates = []
        for r in rows:
            row = dict(r)
            v = derive_row(row, args.cutoff, args.image_dims, snapshot_root)
            tallies[v["label_source"]] += 1
            era_tally[(v["era"], v["crop_valid"])] += 1
            if v["image_w"] is not None:
                n_dims += 1
            updates.append(tuple(v[name] for name in cdb.PROVENANCE_COLUMN_NAMES) + (row["id"],))
        last_id = rows[-1]["id"]
        n_rows += len(rows)
        n_batches += 1
        if not args.dry_run:
            conn.executemany(update_sql, updates)
            conn.commit()
        print(f"  batch {n_batches}: {len(rows)} rows through id {last_id}"
              f"{' (dry-run)' if args.dry_run else ''}")
        if args.max_batches and n_batches >= args.max_batches:
            print(f"stopping after {n_batches} batch(es) (--max-batches); re-run to continue")
            break

    _print_summary(conn, tallies, era_tally, n_rows, n_dims, time.monotonic() - t0,
                   wrote=not args.dry_run)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
