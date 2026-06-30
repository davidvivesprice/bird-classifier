"""Calibration map: monotonic, bounded, maps known raw_scores to expected P."""
from pipeline.calibration import calibrate


def test_monotonic_non_decreasing():
    prev = -1.0
    for raw in range(0, 256):
        p = calibrate(raw)
        assert 0.0 <= p <= 1.0
        assert p >= prev - 1e-9, f"non-monotonic at raw={raw}: {p} < {prev}"
        prev = p


def test_known_points():
    # Under-confident low/mid range, trustworthy high range (from the fit).
    assert calibrate(0) < 0.45            # near-zero -> ~0.36
    assert 0.40 <= calibrate(30) <= 0.55  # raw 30 -> ~0.46 (NOT 30/255=0.12)
    assert calibrate(60) >= 0.70          # raw 60 -> ~0.77 (lockable)
    assert calibrate(255) >= 0.95         # saturates near 1.0


def test_lock_threshold_boundary():
    # The 0.70 lock gate should admit raw>=53 (~0.77) and reject the very low end.
    assert calibrate(53) >= 0.70
    assert calibrate(20) < 0.70
