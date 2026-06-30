"""Post-hoc calibration of the AIY classifier's raw_score -> P(correct).

The AIY model is under-confident and mis-calibrated on feeder crops: at a shown
"0.30" it is actually ~46% likely correct, and a shown "1.0" is ~84%. This maps
the model's raw_score to a calibrated probability so displayed confidence is
honest ("70%" => right ~70%) and the vote-lock can gate on TRUE probability
instead of a guessed raw threshold.

Fit by tools/fit_calibration.py via isotonic regression (PAVA) on the human
reviews. 5-fold CV ECE: 0.182 (raw/100) -> 0.057 (calibrated). Regenerate as
reviews accumulate. Needs no model retrain and no GPU — pure post-hoc.
"""
from __future__ import annotations
from bisect import bisect_right

# (raw_score_threshold, calibrated P(correct)) breakpoints, ascending raw_score.
# Generated 2026-06-29 from 1,875 reviewed crops. Regenerate via tools/fit_calibration.py.
_BREAKPOINTS = [
    (0.0, 0.36), (13.0, 0.45), (19.0, 0.46), (53.0, 0.77), (165.0, 0.79),
    (181.0, 0.80), (183.0, 0.81), (194.0, 0.88), (225.0, 0.90), (247.0, 0.94),
    (248.0, 0.96), (254.0, 1.00),
]
_THRESH = [b[0] for b in _BREAKPOINTS]
_PROBS = [b[1] for b in _BREAKPOINTS]


def calibrate(raw_score: float) -> float:
    """Map an AIY raw_score (0-255) to a calibrated P(correct) in [0,1]."""
    i = bisect_right(_THRESH, raw_score) - 1
    if i < 0:
        return _PROBS[0]
    return _PROBS[i]
