"""Baseline scorer — score existing classifier predictions against the
1,673-review hold-out, WITHOUT running any model inference.

The pipeline logs every AIY prediction into `classifications.db`:
- `common_name`: AIY's top-1 species
- `raw_score`: AIY's raw score (0-255 from the MobileNet output)
- `top3_json`: top-3 predictions

The `reviews` table has ground truth:
- `verdict='correct'`  → AIY's top-1 IS the truth
- `verdict='wrong' AND correct_species=X` → AIY was wrong, truth is X
- `verdict='trash'` → not a bird

So we can compute AIY's historical accuracy on the review hold-out by joining
these two tables — no Coral, no model loading, no production risk. Pure SQL
+ Python analytics.

Yard-model baseline scoring (a separate question — yard's live-overlay
predictions don't land in the DB) requires running yard inference on each
hold-out JPG. That path is stubbed out here but not activated by default
because it contends with production Coral USB. Run with `--yard` only
during a pipeline pause.

Usage:
    python3 -m tier2_eval.baseline                  # AIY baseline, full report
    python3 -m tier2_eval.baseline --top-n 14       # only score on top-14 species
    python3 -m tier2_eval.baseline --json           # machine-readable output
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Optional

import numpy as np

from tier2_eval.metrics import (
    BootstrapCI,
    bootstrap_ci,
    confusion_matrix,
    expected_calibration_error,
    macro_f1,
    per_class_precision,
    per_class_recall,
)

DB = Path.home() / "bird-snapshots" / "logs" / "classifications.db"


# The flagship's 14 in-scope species (from tier2-training-plan-v1). Everything
# else maps to `unknown` for evaluation purposes.
FLAGSHIP_SPECIES = [
    "Black-capped Chickadee", "House Finch", "Northern Cardinal",
    "Dark-eyed Junco", "Mourning Dove", "Song Sparrow",
    "Downy Woodpecker", "Hairy Woodpecker", "Tufted Titmouse",
    "White-breasted Nuthatch", "American Goldfinch", "Carolina Wren",
    "Blue Jay", "Brown-headed Cowbird",
]
NOT_A_BIRD = "not_a_bird"
UNKNOWN = "unknown"


def _bucket(sp: Optional[str]) -> str:
    """Map any species name to one of {14 flagship species, not_a_bird, unknown}."""
    if sp is None or sp == "":
        return UNKNOWN
    if sp in FLAGSHIP_SPECIES:
        return sp
    if sp in (NOT_A_BIRD, "not a bird"):
        return NOT_A_BIRD
    return UNKNOWN


def load_holdout() -> list[dict]:
    """Join reviews + classifications to get ground-truth rows.

    Every usable review has:
      - file
      - truth species (from correct_species if wrong, else common_name)
      - aiy_pred (from classifications.common_name)
      - aiy_conf (from raw_score, normalized to [0, 1])
      - verdict
    """
    if not DB.exists():
        raise RuntimeError(f"classifications.db not found at {DB}")
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT
            r.file,
            r.verdict,
            r.correct_species,
            c.common_name AS aiy_pred,
            c.raw_score AS aiy_raw,
            c.confidence AS detector_conf,
            c.source_timestamp,
            c.camera
        FROM reviews r
        JOIN classifications c ON r.file = c.file
        WHERE r.verdict IN ('correct', 'wrong', 'trash')
          AND c.common_name IS NOT NULL
    """).fetchall()
    conn.close()

    out = []
    for r in rows:
        verdict = r["verdict"]
        if verdict == "correct":
            truth = r["aiy_pred"]
        elif verdict == "wrong" and r["correct_species"]:
            truth = r["correct_species"]
        elif verdict == "trash":
            truth = NOT_A_BIRD
        else:
            continue
        # Normalize raw_score to [0, 1] (it's 0-255 from AIY MobileNet)
        raw = r["aiy_raw"] or 0
        aiy_conf = float(raw) / 255.0 if raw > 1 else float(raw)
        out.append({
            "file": r["file"],
            "truth": truth,
            "aiy_pred": r["aiy_pred"],
            "aiy_conf": aiy_conf,
            "detector_conf": r["detector_conf"] or 0.0,
            "verdict": verdict,
            "source_timestamp": r["source_timestamp"],
            "camera": r["camera"] or "feeder",  # default for legacy rows
        })
    return out


def score_aiy(holdout: list[dict], closed_set: bool = True) -> dict:
    """Score AIY's predictions on the hold-out. When `closed_set` is True,
    everything outside the flagship 14 species maps to `unknown` for scoring.
    """
    y_true_raw = [r["truth"] for r in holdout]
    y_pred_raw = [r["aiy_pred"] for r in holdout]
    confidences = np.array([r["aiy_conf"] for r in holdout])

    if closed_set:
        y_true = [_bucket(t) for t in y_true_raw]
        y_pred = [_bucket(p) for p in y_pred_raw]
        classes = FLAGSHIP_SPECIES + [NOT_A_BIRD, UNKNOWN]
    else:
        y_true = y_true_raw
        y_pred = y_pred_raw
        classes = sorted(set(y_true) | set(y_pred))

    correct = np.array([1 if t == p else 0 for t, p in zip(y_true, y_pred)])

    # Summary metrics
    n = len(holdout)
    top1_acc = float(correct.mean()) if n else 0.0
    mf1 = macro_f1(y_true, y_pred)

    # Per-class recall + precision
    recalls = per_class_recall(y_true, y_pred)
    precisions = per_class_precision(y_true, y_pred)

    # Bootstrap CI on top-1 accuracy
    acc_ci = bootstrap_ci(correct.astype(float), np.mean, n_iter=1000, seed=42)

    # ECE
    ece = expected_calibration_error(confidences, correct, n_bins=10)

    # Per-class counts
    support_true = Counter(y_true)
    support_pred = Counter(y_pred)

    # Confusion matrix (only for flagship species, keeps report readable)
    cm = confusion_matrix(y_true, y_pred, classes)

    return {
        "n": n,
        "top1_accuracy": top1_acc,
        "top1_accuracy_ci": (acc_ci.low, acc_ci.high),
        "macro_f1": mf1,
        "ece": ece,
        "per_class_recall": recalls,
        "per_class_precision": precisions,
        "support_true": dict(support_true),
        "support_pred": dict(support_pred),
        "classes": classes,
        "confusion_matrix": cm.tolist(),
    }


def print_report(name: str, report: dict):
    print(f"\n━━━ {name} ━━━")
    print(f"N samples:                {report['n']}")
    lo, hi = report["top1_accuracy_ci"]
    print(f"Top-1 accuracy:           {report['top1_accuracy']:.3f} "
          f"(95% CI: {lo:.3f}–{hi:.3f})")
    print(f"Macro-F1:                 {report['macro_f1']:.3f}")
    print(f"ECE (bin=10):             {report['ece']:.3f}")
    print()
    print("Per-class recall (sorted by recall):")
    print(f"  {'class':<30s} {'recall':>7s} {'prec':>7s} {'support':>8s}")
    for c, r in sorted(report["per_class_recall"].items(), key=lambda x: -x[1]):
        p = report["per_class_precision"].get(c, 0.0)
        s = report["support_true"].get(c, 0)
        print(f"  {c:<30s} {r:>7.3f} {p:>7.3f} {s:>8d}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true",
                    help="emit the full report as JSON to stdout")
    ap.add_argument("--open-set", action="store_true",
                    help="don't bucket into flagship classes; score raw labels")
    args = ap.parse_args()

    try:
        holdout = load_holdout()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Loaded {len(holdout)} hold-out samples from reviews+classifications join.",
          file=sys.stderr)

    report = score_aiy(holdout, closed_set=not args.open_set)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print_report(
            "AIY baseline — " + ("open-set" if args.open_set else "16-class flagship"),
            report,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
