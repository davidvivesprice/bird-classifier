"""Tests for tier2_eval.metrics — TDD first."""
import numpy as np
import pytest

from tier2_eval.metrics import (
    macro_f1,
    per_class_recall,
    per_class_precision,
    confusion_matrix,
    expected_calibration_error,
    bootstrap_ci,
    ood_auroc,
    fpr_at_tpr,
)


# ── macro_f1 ──────────────────────────────────────────────────────────────


def test_macro_f1_perfect_prediction():
    y_true = ["A", "B", "A", "B", "C"]
    y_pred = ["A", "B", "A", "B", "C"]
    f1 = macro_f1(y_true, y_pred)
    assert f1 == pytest.approx(1.0)


def test_macro_f1_all_wrong():
    y_true = ["A", "B", "A"]
    y_pred = ["B", "A", "B"]
    f1 = macro_f1(y_true, y_pred)
    assert f1 == pytest.approx(0.0)


def test_macro_f1_ignores_class_imbalance_unlike_micro():
    # 99 correct A's, 1 wrong B → macro should be ~0.5 (A: 1.0, B: 0.0)
    # Micro would be 0.99.
    y_true = ["A"] * 99 + ["B"]
    y_pred = ["A"] * 99 + ["A"]
    f1 = macro_f1(y_true, y_pred)
    # B has precision=0 (never predicted), recall=0 (never correct) → F1=0
    # A has precision=99/100, recall=99/99 → F1 ≈ 0.995
    # macro = (0.995 + 0) / 2 ≈ 0.497
    assert 0.4 < f1 < 0.55


def test_macro_f1_three_class_mixed():
    y_true = ["A", "A", "A", "B", "B", "C"]
    y_pred = ["A", "A", "B", "B", "A", "C"]
    # A: tp=2, fp=1, fn=1 → p=2/3 r=2/3 F1=0.667
    # B: tp=1, fp=1, fn=1 → p=0.5 r=0.5 F1=0.5
    # C: tp=1, fp=0, fn=0 → F1=1.0
    # macro = (0.667 + 0.5 + 1.0) / 3 = 0.722
    f1 = macro_f1(y_true, y_pred)
    assert f1 == pytest.approx(0.7222, abs=0.001)


# ── per_class_recall / per_class_precision ────────────────────────────────


def test_per_class_recall():
    y_true = ["A", "A", "A", "B", "B", "C"]
    y_pred = ["A", "A", "B", "B", "A", "C"]
    r = per_class_recall(y_true, y_pred)
    assert r["A"] == pytest.approx(2 / 3)
    assert r["B"] == pytest.approx(0.5)
    assert r["C"] == pytest.approx(1.0)


def test_per_class_precision_zero_when_class_never_predicted():
    y_true = ["A", "B"]
    y_pred = ["A", "A"]  # B never predicted
    p = per_class_precision(y_true, y_pred)
    assert p["A"] == pytest.approx(0.5)
    assert p["B"] == pytest.approx(0.0)  # never predicted → precision 0 by convention


# ── confusion_matrix ───────────────────────────────────────────────────────


def test_confusion_matrix_basic():
    y_true = ["A", "A", "B", "B"]
    y_pred = ["A", "B", "B", "A"]
    classes = ["A", "B"]
    cm = confusion_matrix(y_true, y_pred, classes)
    # rows = true, cols = pred
    # A→A: 1, A→B: 1
    # B→A: 1, B→B: 1
    assert cm.shape == (2, 2)
    assert cm[0, 0] == 1
    assert cm[0, 1] == 1
    assert cm[1, 0] == 1
    assert cm[1, 1] == 1


def test_confusion_matrix_unknown_class_ignored():
    y_true = ["A", "A"]
    y_pred = ["A", "Z"]  # Z not in classes → goes to unknown column
    classes = ["A"]
    cm = confusion_matrix(y_true, y_pred, classes)
    assert cm[0, 0] == 1
    # When a predicted class is unknown we drop that row — documented behavior.


# ── expected_calibration_error ─────────────────────────────────────────────


def test_ece_perfectly_calibrated():
    # If model says 80%, it's right 80% of the time → ECE = 0
    # Fake 100 predictions at 0.8 confidence, 80 correct
    confidences = np.array([0.8] * 100)
    correct = np.array([1] * 80 + [0] * 20)
    ece = expected_calibration_error(confidences, correct, n_bins=10)
    assert ece == pytest.approx(0.0, abs=0.01)


def test_ece_overconfident():
    # Model says 99% but right only 50% → big ECE
    confidences = np.array([0.99] * 100)
    correct = np.array([1] * 50 + [0] * 50)
    ece = expected_calibration_error(confidences, correct, n_bins=10)
    # |0.99 - 0.50| ≈ 0.49
    assert ece == pytest.approx(0.49, abs=0.02)


def test_ece_monotonic_in_gap():
    # More gap between confidence and accuracy → more ECE
    conf_low = np.array([0.6] * 100)
    correct_low = np.array([1] * 55 + [0] * 45)  # 55% correct
    conf_hi = np.array([0.99] * 100)
    correct_hi = np.array([1] * 50 + [0] * 50)
    ece_low = expected_calibration_error(conf_low, correct_low, n_bins=10)
    ece_hi = expected_calibration_error(conf_hi, correct_hi, n_bins=10)
    assert ece_hi > ece_low


# ── bootstrap_ci ───────────────────────────────────────────────────────────


def test_bootstrap_ci_returns_mean_and_interval():
    data = np.array([0.0, 1.0] * 50)  # 50% mean
    ci = bootstrap_ci(data, np.mean, n_iter=500, alpha=0.05, seed=42)
    assert ci.point == pytest.approx(0.5, abs=0.01)
    assert ci.low <= ci.point <= ci.high
    assert ci.low > 0.35
    assert ci.high < 0.65


def test_bootstrap_ci_tight_for_identical_data():
    data = np.array([0.7] * 200)
    ci = bootstrap_ci(data, np.mean, n_iter=500, alpha=0.05, seed=42)
    assert ci.low == pytest.approx(0.7)
    assert ci.high == pytest.approx(0.7)
    assert ci.point == pytest.approx(0.7)


# ── OOD metrics ────────────────────────────────────────────────────────────


def test_ood_auroc_perfect_separation():
    # Lower score = more OOD (by convention for energy scoring)
    # ID samples: scores 0.8-1.0, OOD samples: scores 0.0-0.2
    id_scores = np.linspace(0.8, 1.0, 50)
    ood_scores = np.linspace(0.0, 0.2, 50)
    auroc = ood_auroc(id_scores, ood_scores)
    assert auroc == pytest.approx(1.0)


def test_ood_auroc_no_separation():
    # Both distributions identical → AUROC 0.5
    rng = np.random.default_rng(seed=42)
    id_scores = rng.normal(0.5, 0.1, 1000)
    ood_scores = rng.normal(0.5, 0.1, 1000)
    auroc = ood_auroc(id_scores, ood_scores)
    assert auroc == pytest.approx(0.5, abs=0.05)


def test_fpr_at_tpr_matches_threshold_semantics():
    # Build a case where TPR=95% requires accepting the lowest-ID-scoring 95%
    # and see what fraction of OOD has score above that threshold.
    id_scores = np.linspace(0.1, 1.0, 100)    # 100 ID samples, scores 0.1 to 1.0
    ood_scores = np.linspace(0.0, 0.5, 100)   # 100 OOD samples
    # At 95% TPR we accept ID scores >= some threshold around 0.15
    # All OOD with score > 0.15 = FPs. Roughly 0.5-0.15 / 0.5 = 70% of OOD
    fpr = fpr_at_tpr(id_scores, ood_scores, tpr=0.95)
    assert 0.5 < fpr < 0.9


def test_fpr_at_tpr_perfect_separation_gives_zero_fpr():
    id_scores = np.linspace(0.8, 1.0, 100)
    ood_scores = np.linspace(0.0, 0.2, 100)
    fpr = fpr_at_tpr(id_scores, ood_scores, tpr=0.95)
    assert fpr == pytest.approx(0.0)
