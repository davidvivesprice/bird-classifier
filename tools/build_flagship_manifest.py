#!/usr/bin/env python3
"""Build the flagship-classifier training manifest (NON-MUTATING).

Reads the iMac data store (classified/ image folders + classifications.db reviews)
and emits a manifest CSV: path,label,split,source. It does NOT move or modify any
image — it only indexes them and assigns splits.

Design (see docs/working/specs/2026-06-29-flagship-classifier-design.md):
- CLASS_SET = the 15 feeder species with >=20 clean reviews, + not_a_bird.
- Human reviews are GROUND TRUTH and are QUARANTINED to the test splits — never train.
- train/val come from the (unreviewed) weak AIY-labeled classified/ folders.
  val = a per-class capped sample (early-stopping MONITOR only); the data is too
  bursty for clean day-grouping at the val size, and the leakage-free eval is the
  quarantined human-review TEST set, so minor within-visit train/val overlap is OK.
- test = reviewed files (clean labels). ood_test = reviewed birds whose species
  is NOT in CLASS_SET (resolve to "unknown"). NOTE: trash-review images are
  deleted on the iMac, so there is no clean not_a_bird test set from reviews —
  OOD rejection leans on the design's Mahalanobis distance gate, not a not_a_bird class.
- not_a_bird train data comes from the repo's dataset_negatives/ (currently only
  ~34 imgs — thin; flagged as a data gap for David).

Usage: python3 tools/build_flagship_manifest.py [--out PATH]
Run on the iMac (where ~/bird-snapshots lives). Stdlib + sqlite3 only.
"""
from __future__ import annotations
import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

HOME = Path.home()
CLASSIFIED = HOME / "bird-snapshots" / "classified"
NEGATIVES = Path(__file__).resolve().parent.parent / "dataset_negatives"  # repo-side
DB = HOME / "bird-snapshots" / "logs" / "classifications.db"

CLASS_SET = {
    "House Finch", "Black-capped Chickadee", "Carolina Wren", "Hairy Woodpecker",
    "Song Sparrow", "American Goldfinch", "Downy Woodpecker", "Dark-eyed Junco",
    "White-breasted Nuthatch", "Tufted Titmouse", "Northern Cardinal",
    "Brown-headed Cowbird", "Red-bellied Woodpecker", "Mourning Dove", "Blue Jay",
}
NOT_A_BIRD = "not_a_bird"
UNKNOWN = "unknown"

_date_re = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def norm(name: str) -> str:
    return name.replace("_", " ").strip()


def date_of(filename: str) -> str:
    m = _date_re.match(filename)
    return m.group(1) if m else "unknown-date"


def index_classified() -> dict:
    """filename -> (fullpath, weak_label). Last writer wins on dual dirs; both
    dirs normalize to the same label so it doesn't matter which path we keep."""
    idx = {}
    if not CLASSIFIED.is_dir():
        print(f"ERROR: {CLASSIFIED} not found", file=sys.stderr)
        sys.exit(1)
    for folder in CLASSIFIED.iterdir():
        if not folder.is_dir():
            continue
        label = norm(folder.name)
        for img in folder.iterdir():
            if img.suffix.lower() in (".jpg", ".jpeg", ".png"):
                idx[img.name] = (str(img), label)
    return idx


def load_reviews() -> dict:
    """filename -> (verdict, correct_species)."""
    if not DB.exists():
        print(f"ERROR: {DB} not found", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT file, verdict, COALESCE(correct_species,'') FROM reviews"
    ).fetchall()
    con.close()
    out = {}
    for f, verdict, corr in rows:
        if f:
            out[f] = (verdict, corr)
    return out


def load_bboxes() -> dict:
    """filename -> 'x1 y1 x2 y2' (full-frame coords) from best_detection_json.

    Birds are full frames, often multi-bird, so training MUST crop to the
    classified bird's bbox. best_detection_json is the highest-confidence
    detection AIY classified. Files without a bbox (e.g. negatives) -> absent.
    """
    import json
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT file, best_detection_json FROM classifications "
        "WHERE best_detection_json IS NOT NULL AND best_detection_json<>''"
    ).fetchall()
    con.close()
    out = {}
    for f, bj in rows:
        if not f or f in out:
            continue
        try:
            box = json.loads(bj).get("box")
            if box and len(box) == 4:
                out[f] = " ".join(str(int(v)) for v in box)
        except Exception:
            pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HOME / "bird-snapshots" / "flagship" / "manifest.csv"))
    ap.add_argument("--val-frac", type=float, default=0.15)
    args = ap.parse_args()

    idx = index_classified()
    reviews = load_reviews()
    bboxes = load_bboxes()
    print(f"indexed {len(idx)} classified images; {len(reviews)} reviews; {len(bboxes)} bboxes")

    rows = []  # (path, label, split, source, bbox)
    counts = defaultdict(Counter)  # split -> label -> n
    pool = defaultdict(lambda: defaultdict(list))  # label -> day -> [path] (train candidates)

    for fname, (path, weak_label) in idx.items():
        rev = reviews.get(fname)
        if rev is not None:
            # GROUND TRUTH -> test/ood_test only (quarantined from train).
            verdict, corr = rev
            if verdict == "trash":
                label, split = NOT_A_BIRD, "ood_test"
            elif verdict == "correct":
                label = weak_label  # AIY folder was confirmed correct
                split = "test" if label in CLASS_SET else "ood_test"
                if label not in CLASS_SET:
                    label = UNKNOWN
            elif verdict == "reclassify" and corr:
                label = norm(corr)
                split = "test" if label in CLASS_SET else "ood_test"
                if label not in CLASS_SET:
                    label = UNKNOWN
            else:
                continue  # wrong / skip / requeued -> omit (ambiguous)
            rows.append((path, label, split, f"review:{verdict}", bboxes.get(fname, "")))
            counts[split][label] += 1
        elif weak_label in CLASS_SET:
            pool[weak_label][date_of(fname)].append(path)
        # non-core unreviewed -> excluded from train (weak 'unknown' too noisy)

    # not_a_bird training pool from dataset_negatives/
    if NEGATIVES.is_dir():
        for img in NEGATIVES.rglob("*"):
            if img.suffix.lower() in (".jpg", ".jpeg", ".png"):
                pool[NOT_A_BIRD][date_of(img.name)].append(str(img))

    # Per-class capped val sample. The data is extremely bursty (a single visit
    # can produce 10k+ frames in one day), so whole-day grouping bloats val
    # uncontrollably. Val is only an early-stopping MONITOR — the authoritative,
    # leakage-free eval is the quarantined human-review TEST set. So val = a
    # per-class deterministic sample of min(val_frac, VAL_CAP) files, balanced
    # across all classes. (Accepts minor within-visit train/val leakage, which
    # does not affect the TEST metric that decides ship/no-ship.)
    VAL_CAP = 300
    for label, days in pool.items():
        files = sorted((p for paths in days.values() for p in paths), key=lambda p: hash(p))
        n_val = min(int(len(files) * args.val_frac), VAL_CAP)
        src = "negatives" if label == NOT_A_BIRD else "weak_aiy"
        for i, p in enumerate(files):
            split = "val" if i < n_val else "train"
            rows.append((p, label, split, src, bboxes.get(Path(p).name, "")))
            counts[split][label] += 1

    # Leakage assertion: no reviewed filename may appear in train/val.
    train_files = {Path(p).name for p, l, s, src, bb in rows if s in ("train", "val")}
    leak = len(train_files & set(reviews.keys()))
    bird_rows = [r for r in rows if r[1] not in (NOT_A_BIRD,)]
    with_bbox = sum(1 for r in bird_rows if r[4])

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "label", "split", "source", "bbox"])
        w.writerows(rows)

    print(f"\nwrote {len(rows)} rows -> {out}")
    print(f"LEAKAGE CHECK (reviewed files in train/val): {leak}  {'OK' if leak == 0 else '!!! FAIL'}")
    print(f"BBOX coverage (bird rows with a crop box): {with_bbox}/{len(bird_rows)} "
          f"({100*with_bbox/max(len(bird_rows),1):.0f}%) — rest fall back to full frame")
    for split in ("train", "val", "test", "ood_test"):
        c = counts[split]
        print(f"\n[{split}] total={sum(c.values())}")
        for label, n in sorted(c.items(), key=lambda x: -x[1]):
            print(f"   {n:6d}  {label}")


if __name__ == "__main__":
    main()
