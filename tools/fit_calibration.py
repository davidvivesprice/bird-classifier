#!/usr/bin/env python3
"""Fit the AIY raw_score -> P(correct) calibration map from human reviews.

Isotonic regression (PAVA) on (raw_score, was_correct) pairs joined from the
reviews + classifications tables. Reports 5-fold CV ECE (current vs calibrated)
and prints the compact breakpoints to paste into pipeline/calibration.py.

Run on the iMac (where classifications.db lives). Stdlib + sqlite3 only.
Regenerate whenever the review count grows materially.
"""
from __future__ import annotations
import collections
import random
import sqlite3
from pathlib import Path

DB = Path.home() / "bird-snapshots" / "logs" / "classifications.db"


def load_pairs():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT c.raw_score, CASE WHEN r.verdict='correct' THEN 1 ELSE 0 END "
        "FROM reviews r JOIN classifications c ON r.file=c.file "
        "WHERE r.verdict IN ('correct','wrong','reclassify') AND c.raw_score IS NOT NULL"
    ).fetchall()
    con.close()
    return [(float(a), int(b)) for a, b in rows]


def pava(pairs):
    """Isotonic regression -> ascending [(raw_threshold, calibrated_P)] breakpoints."""
    pairs = sorted(pairs)
    bx = [r for r, _ in pairs]
    y = [c for _, c in pairs]
    w = [1.0] * len(y)
    i = 0
    while i < len(y) - 1:
        if y[i] > y[i + 1]:
            y[i] = (y[i] * w[i] + y[i + 1] * w[i + 1]) / (w[i] + w[i + 1])
            w[i] += w[i + 1]
            del y[i + 1], w[i + 1], bx[i + 1]
            if i > 0:
                i -= 1
        else:
            i += 1
    bp = []
    for rawx, py in zip(bx, y):
        if not bp or abs(bp[-1][1] - py) > 1e-9:
            bp.append((rawx, round(py, 4)))
    return bp


def apply_map(bp, raw):
    ans = bp[0][1]
    for rx, py in bp:
        if raw >= rx:
            ans = py
        else:
            break
    return ans


def ece(pairs, fn, bins=10):
    b = collections.defaultdict(list)
    for raw, c in pairs:
        b[min(int(fn(raw) * bins), bins - 1)].append((fn(raw), c))
    n = len(pairs)
    e = 0.0
    for v in b.values():
        if v:
            ac = sum(p for p, _ in v) / len(v)
            acc = sum(c for _, c in v) / len(v)
            e += abs(ac - acc) * len(v) / n
    return e


def main():
    rows = load_pairs()
    cur = lambda raw: min(raw / 100.0, 1.0)
    random.seed(1)
    idx = list(range(len(rows)))
    random.shuffle(idx)
    folds = [idx[i::5] for i in range(5)]
    cur_e, cal_e = [], []
    for k in range(5):
        te = set(folds[k])
        tr = [rows[i] for i in idx if i not in te]
        tst = [rows[i] for i in folds[k]]
        bp = pava(tr)
        cur_e.append(ece(tst, cur))
        cal_e.append(ece(tst, lambda r: apply_map(bp, r)))
    print(f"n={len(rows)}")
    print(f"5-fold CV ECE  current={sum(cur_e)/5:.3f}  calibrated={sum(cal_e)/5:.3f}")
    bp = pava(rows)
    print("\nPaste into pipeline/calibration.py _BREAKPOINTS:")
    print("[" + ", ".join(f"({r:.1f}, {p:.2f})" for r, p in bp) + "]")


if __name__ == "__main__":
    main()
