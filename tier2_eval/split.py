"""Visit-grouped splits for camera-trap ML.

The camera-trap ML literature (Beery 2018 iWildCam; Schneider 2020) is
consistent: naive random train/test splitting inflates accuracy by 5-20%
because two crops from the same 5-minute visit are near-duplicates.

This module produces splits where no visit appears on both sides of any
fold. Uses scikit-learn's StratifiedGroupKFold under the hood (available
1.0+), which preserves both the no-leakage constraint AND the label
distribution across folds.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable, Iterator, Sequence

import numpy as np


class LeakageError(Exception):
    """Raised when a visit appears in both train and test of a fold."""


def derive_visit_ids(
    records: Sequence[dict],
    window_seconds: int = 300,
) -> list[int]:
    """Assign a visit_id to each record by clustering within `window_seconds`.

    Algorithm:
      1. Sort records by (camera, source_timestamp).
      2. Walk in order. Start a new visit_id whenever the camera changes OR
         the gap from the previous record exceeds `window_seconds`.
      3. Return visit_ids in ORIGINAL input order (not sorted order).

    Parameters
    ----------
    records : sequence of dicts, each with at least "camera" and
              "source_timestamp" keys. Timestamp may be ISO-8601 string,
              `datetime`, or float seconds since epoch.
    window_seconds : gap above which a new visit starts. 300 = 5 minutes.

    Returns
    -------
    list[int] : one visit_id per input record, same length, same order.
    """
    def _parse_ts(ts):
        if isinstance(ts, (int, float)):
            return float(ts)
        if isinstance(ts, datetime):
            return ts.timestamp()
        s = str(ts)
        # Observed noise in real data: trailing "_N" suffix (bird_index?) and
        # space-separated date+time instead of 'T'. Strip both.
        if "_" in s:
            s = s.split("_")[0]
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            s2 = s.split(".")[0].replace(" ", "T")
            try:
                dt = datetime.fromisoformat(s2)
            except ValueError:
                # Last-ditch: try to parse YYYY-MM-DD HH:MM:SS directly
                from datetime import datetime as _dt
                dt = _dt.strptime(s2[:19], "%Y-%m-%dT%H:%M:%S")
        return dt.timestamp()

    n = len(records)
    indexed = [(i, r.get("camera", ""), _parse_ts(r["source_timestamp"]))
               for i, r in enumerate(records)]
    indexed.sort(key=lambda x: (x[1], x[2]))

    visit_ids = [0] * n
    current_id = -1
    prev_cam = None
    prev_ts = None
    for orig_idx, cam, ts in indexed:
        if prev_cam is None or cam != prev_cam or (ts - prev_ts) > window_seconds:
            current_id += 1
        visit_ids[orig_idx] = current_id
        prev_cam = cam
        prev_ts = ts
    return visit_ids


def stratified_group_kfold(
    labels: Sequence,
    visit_ids: Sequence,
    n_splits: int = 5,
    seed: int = 0,
) -> Iterator[tuple[np.ndarray, np.ndarray]]:
    """Yield (train_indices, test_indices) for each of `n_splits` folds.

    Guarantees:
    - No visit appears in both train and test of any fold.
    - Label distribution in each test fold is approximately the overall.
    - Every sample appears in exactly one test fold.

    Uses scikit-learn's StratifiedGroupKFold.
    """
    from sklearn.model_selection import StratifiedGroupKFold
    labels = np.asarray(labels)
    visit_ids = np.asarray(visit_ids)
    sgkf = StratifiedGroupKFold(
        n_splits=n_splits, shuffle=True, random_state=seed,
    )
    for train_idx, test_idx in sgkf.split(np.zeros(len(labels)), labels, visit_ids):
        yield train_idx, test_idx


def assert_no_visit_leakage(
    train_visits: Iterable,
    test_visits: Iterable,
) -> None:
    """Raise LeakageError if any visit is in both sets.

    The ONE assert to run before every training call. Fail loud, fail fast.
    """
    train_set = set(train_visits)
    test_set = set(test_visits)
    overlap = train_set & test_set
    if overlap:
        raise LeakageError(
            f"Visit-level leakage detected: {len(overlap)} visits in both "
            f"train and test. First few: {sorted(list(overlap))[:5]}"
        )
