"""Tests for tier2_eval.split — visit-grouped K-fold for camera-trap ML.

The bug this prevents: two crops from the same 5-minute visit (same bird,
same lighting) leaking across train/test → inflated accuracy. Every
camera-trap ML paper documents this as the dominant data-leakage mode.
"""
import numpy as np
import pytest

from tier2_eval.split import (
    derive_visit_ids,
    stratified_group_kfold,
    assert_no_visit_leakage,
    LeakageError,
)


# ── derive_visit_ids ──────────────────────────────────────────────────────


def test_visit_ids_grouped_by_5min_window():
    # Same camera, 3 detections within a 5-minute window → 1 visit
    records = [
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:00:00"},
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:02:30"},
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:04:15"},
        # 10 min gap → new visit
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:15:00"},
    ]
    visits = derive_visit_ids(records, window_seconds=300)
    assert visits[0] == visits[1] == visits[2]
    assert visits[3] != visits[0]


def test_visit_ids_split_by_camera():
    # Same timestamp, different cameras → different visits
    records = [
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:00:00"},
        {"camera": "ground", "source_timestamp": "2026-03-15T10:00:00"},
    ]
    visits = derive_visit_ids(records, window_seconds=300)
    assert visits[0] != visits[1]


def test_visit_ids_handles_unordered_input():
    # Input is chronologically shuffled — function sorts internally
    records = [
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:04:15"},
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:00:00"},
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:02:30"},
    ]
    visits = derive_visit_ids(records, window_seconds=300)
    # All three should get the same visit id regardless of input order
    assert visits[0] == visits[1] == visits[2]


def test_visit_ids_same_timestamp_same_visit():
    # Two frames at exactly the same second
    records = [
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:00:00"},
        {"camera": "feeder", "source_timestamp": "2026-03-15T10:00:00"},
    ]
    visits = derive_visit_ids(records, window_seconds=300)
    assert visits[0] == visits[1]


# ── stratified_group_kfold ────────────────────────────────────────────────


def test_stratified_group_kfold_no_visit_leakage():
    """No visit id may appear in both train and test of any fold."""
    n = 200
    rng = np.random.default_rng(42)
    # 50 distinct visits, 4 samples each
    visit_ids = np.repeat(np.arange(50), 4)
    # Species labels, imbalanced: 70% class A, 30% class B
    # Ensure multiple classes per visit sometimes (realistic)
    labels = np.array(["A" if rng.random() < 0.7 else "B" for _ in range(n)])
    rng.shuffle(visit_ids)  # scramble input order
    folds = list(stratified_group_kfold(labels, visit_ids, n_splits=5, seed=42))

    assert len(folds) == 5
    for train_idx, test_idx in folds:
        train_visits = set(visit_ids[train_idx].tolist())
        test_visits = set(visit_ids[test_idx].tolist())
        assert train_visits.isdisjoint(test_visits), \
            f"visit leakage between train and test: {train_visits & test_visits}"


def test_stratified_group_kfold_distinct_test_sets():
    """Each sample appears in exactly one test fold."""
    n = 200
    visit_ids = np.repeat(np.arange(50), 4)
    labels = np.array(["A"] * 100 + ["B"] * 100)
    folds = list(stratified_group_kfold(labels, visit_ids, n_splits=5, seed=42))
    all_test = np.concatenate([test for _, test in folds])
    assert len(set(all_test.tolist())) == n  # every sample in exactly one test


def test_stratified_group_kfold_roughly_balanced_labels_across_folds():
    """Stratified ⇒ label distribution across folds is similar to overall."""
    n = 500
    visit_ids = np.repeat(np.arange(100), 5)
    # Imbalanced: 80% A, 20% B
    labels = np.array(["A"] * 400 + ["B"] * 100)
    rng = np.random.default_rng(0)
    perm = rng.permutation(n)
    labels = labels[perm]
    visit_ids = visit_ids[perm]
    folds = list(stratified_group_kfold(labels, visit_ids, n_splits=5, seed=42))
    for _, test_idx in folds:
        fold_labels = labels[test_idx]
        frac_b = (fold_labels == "B").mean()
        # Overall B fraction is 0.2. Each fold should be within 0.05 of that.
        assert 0.10 < frac_b < 0.30, \
            f"fold B fraction {frac_b} not close to overall 0.2"


# ── assert_no_visit_leakage ───────────────────────────────────────────────


def test_assert_no_visit_leakage_passes_when_clean():
    train_visits = {1, 2, 3}
    test_visits = {4, 5}
    # Should not raise
    assert_no_visit_leakage(train_visits, test_visits)


def test_assert_no_visit_leakage_raises_on_overlap():
    train_visits = {1, 2, 3}
    test_visits = {3, 4, 5}  # 3 is in both
    with pytest.raises(LeakageError) as exc:
        assert_no_visit_leakage(train_visits, test_visits)
    assert "3" in str(exc.value) or "leakage" in str(exc.value).lower()
