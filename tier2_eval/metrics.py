"""Classification + calibration + OOD metrics for the Tier 2 evaluation harness.

No production dependencies; pure analytics. Tested in
`tests/tier2_eval/test_metrics.py`.

Conventions:
- `y_true`, `y_pred` are sequences of class labels (strings). No index-encoding
  required — everything works on category names so we don't lose the label
  meaning to downstream reports.
- `confidences` are per-prediction probabilities for the PREDICTED class
  (not the full softmax distribution).
- `correct` is a 0/1 array matching predictions.
- OOD score convention: HIGHER = MORE IN-DISTRIBUTION. Energy score is
  usually `-logsumexp(logits)` which is lower for OOD → we negate it before
  passing in, so downstream code is uniform.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Iterable

import numpy as np


# ── Classification ─────────────────────────────────────────────────────────


def _class_counts(y_true: Iterable[str], y_pred: Iterable[str]):
    """Compute tp/fp/fn per class. Returns dict[class, (tp, fp, fn)]."""
    y_true = list(y_true)
    y_pred = list(y_pred)
    classes = sorted(set(y_true) | set(y_pred))
    counts = {c: [0, 0, 0] for c in classes}  # tp, fp, fn
    for t, p in zip(y_true, y_pred):
        if t == p:
            counts[t][0] += 1
        else:
            counts[p][1] += 1  # false positive for predicted
            counts[t][2] += 1  # false negative for true
    return {c: tuple(v) for c, v in counts.items()}


def per_class_recall(y_true, y_pred) -> dict[str, float]:
    out = {}
    for c, (tp, _fp, fn) in _class_counts(y_true, y_pred).items():
        out[c] = tp / (tp + fn) if (tp + fn) else 0.0
    return out


def per_class_precision(y_true, y_pred) -> dict[str, float]:
    out = {}
    for c, (tp, fp, _fn) in _class_counts(y_true, y_pred).items():
        out[c] = tp / (tp + fp) if (tp + fp) else 0.0
    return out


def macro_f1(y_true, y_pred) -> float:
    """Macro-average F1. Unweighted across classes (imbalance-unaware).

    For heavily imbalanced data this is the right metric — it exposes tail
    classes that micro-F1 would mask. If a class has zero true+predicted
    support it's skipped (can't compute F1); if it has support but zero
    correct predictions, F1 = 0 for that class and it IS included in the
    mean.
    """
    recalls = per_class_recall(y_true, y_pred)
    precisions = per_class_precision(y_true, y_pred)
    f1s = []
    for c in recalls:
        r = recalls[c]
        p = precisions.get(c, 0.0)
        if r + p == 0:
            f1s.append(0.0)
        else:
            f1s.append(2 * p * r / (p + r))
    return sum(f1s) / len(f1s) if f1s else 0.0


def confusion_matrix(y_true, y_pred, classes: list[str]) -> np.ndarray:
    """Confusion matrix with rows=true, cols=pred, restricted to `classes`.

    Samples whose true label isn't in `classes` are dropped. Predictions
    outside `classes` are also dropped (documented behavior — used so the
    matrix visualization stays focused on the target label set).
    """
    idx = {c: i for i, c in enumerate(classes)}
    n = len(classes)
    cm = np.zeros((n, n), dtype=np.int64)
    for t, p in zip(y_true, y_pred):
        ti = idx.get(t)
        pi = idx.get(p)
        if ti is None or pi is None:
            continue
        cm[ti, pi] += 1
    return cm


# ── Calibration ────────────────────────────────────────────────────────────


def expected_calibration_error(
    confidences: np.ndarray,
    correct: np.ndarray,
    n_bins: int = 10,
) -> float:
    """Expected Calibration Error (Guo 2017).

    Partitions predictions by their top-1 confidence into `n_bins` equal-width
    bins over [0, 1]. In each bin, compare mean confidence to empirical
    accuracy. ECE = sum( |conf - acc| * bin_weight ).

    Parameters
    ----------
    confidences : (N,) float array in [0, 1]
    correct : (N,) int/bool array — 1 if top-1 prediction was correct
    n_bins : number of equal-width bins
    """
    confidences = np.asarray(confidences, dtype=float)
    correct = np.asarray(correct, dtype=float)
    if confidences.size == 0:
        return 0.0

    bin_edges = np.linspace(0.0, 1.0, n_bins + 1)
    ece = 0.0
    total = confidences.size
    for i in range(n_bins):
        lo, hi = bin_edges[i], bin_edges[i + 1]
        # Include right edge on the last bin
        if i == n_bins - 1:
            mask = (confidences >= lo) & (confidences <= hi)
        else:
            mask = (confidences >= lo) & (confidences < hi)
        if not mask.any():
            continue
        acc = correct[mask].mean()
        conf = confidences[mask].mean()
        weight = mask.sum() / total
        ece += abs(conf - acc) * weight
    return float(ece)


# ── Bootstrap confidence intervals ─────────────────────────────────────────


@dataclass(frozen=True)
class BootstrapCI:
    point: float   # the point estimate on the full sample
    low: float     # lower percentile
    high: float    # upper percentile
    n_iter: int


def bootstrap_ci(
    data: np.ndarray,
    statistic: Callable,
    n_iter: int = 1000,
    alpha: float = 0.05,
    seed: int = 0,
) -> BootstrapCI:
    """Non-parametric bootstrap CI for the given statistic.

    Returns (point, low, high) for a (1-alpha) two-sided CI. Right for any
    summary metric — accuracy, F1, ECE, AUROC — as long as `statistic`
    takes a 1D array and returns a scalar.
    """
    data = np.asarray(data)
    rng = np.random.default_rng(seed)
    n = data.size
    if n == 0:
        return BootstrapCI(point=0.0, low=0.0, high=0.0, n_iter=n_iter)
    point = float(statistic(data))
    samples = np.empty(n_iter)
    for i in range(n_iter):
        idx = rng.integers(0, n, size=n)
        samples[i] = statistic(data[idx])
    low = float(np.quantile(samples, alpha / 2))
    high = float(np.quantile(samples, 1 - alpha / 2))
    return BootstrapCI(point=point, low=low, high=high, n_iter=n_iter)


# ── OOD detection ──────────────────────────────────────────────────────────


def ood_auroc(id_scores: np.ndarray, ood_scores: np.ndarray) -> float:
    """AUROC for OOD detection.

    Convention: HIGHER score = MORE in-distribution. So a perfect detector
    has every ID score above every OOD score → AUROC = 1.0.

    Implementation: Mann-Whitney U-statistic normalized.
    """
    id_scores = np.asarray(id_scores, dtype=float)
    ood_scores = np.asarray(ood_scores, dtype=float)
    n_id = id_scores.size
    n_ood = ood_scores.size
    if n_id == 0 or n_ood == 0:
        return 0.5
    # For every pair (id, ood), count id > ood (with 0.5 credit for ties)
    # Efficient vectorization via broadcasting would OOM on large arrays;
    # use argsort-based approach.
    combined = np.concatenate([id_scores, ood_scores])
    labels = np.concatenate([np.ones(n_id), np.zeros(n_ood)])
    order = np.argsort(combined, kind="mergesort")
    sorted_labels = labels[order]
    # Rank-based Mann-Whitney
    ranks = np.empty_like(order, dtype=float)
    i = 0
    while i < len(combined):
        j = i
        while j + 1 < len(combined) and combined[order[j + 1]] == combined[order[i]]:
            j += 1
        avg_rank = (i + j) / 2 + 1  # 1-indexed rank
        ranks[i:j + 1] = avg_rank
        i = j + 1
    rank_sum_id = ranks[sorted_labels == 1].sum()
    u = rank_sum_id - n_id * (n_id + 1) / 2
    auroc = u / (n_id * n_ood)
    return float(auroc)


def fpr_at_tpr(
    id_scores: np.ndarray,
    ood_scores: np.ndarray,
    tpr: float = 0.95,
) -> float:
    """FPR when the threshold is set so TPR on in-distribution = `tpr`.

    "Threshold" here is: accept if score >= T. TPR = fraction of ID scores
    >= T. FPR = fraction of OOD scores >= T (false alarms).

    `tpr` is typically 0.95 per the OOD-detection literature convention.
    """
    id_scores = np.asarray(id_scores, dtype=float)
    ood_scores = np.asarray(ood_scores, dtype=float)
    if id_scores.size == 0 or ood_scores.size == 0:
        return 0.0
    # Find the threshold T such that TPR fraction of ID scores are >= T.
    # That's the (1 - tpr)th percentile of id_scores.
    T = float(np.quantile(id_scores, 1 - tpr))
    # How many OOD scores are >= T?
    fp = (ood_scores >= T).sum()
    return float(fp / ood_scores.size)
