"""Tests for MotionGate (region-based)."""
import numpy as np


def test_no_motion_returns_empty_list():
    """Identical frames should produce no motion regions."""
    from pipeline.motion_gate import MotionGate
    gate = MotionGate()
    # First frame warms up background model
    still = np.ones((480, 640, 3), dtype=np.uint8) * 128
    gate.regions(still)
    gate.regions(still)
    regions = gate.regions(still)
    assert regions == []


def test_motion_produces_region():
    """A bright blob appearing on a gray frame should produce a region."""
    from pipeline.motion_gate import MotionGate
    gate = MotionGate()
    # Warm up with gray frames
    gray = np.ones((480, 640, 3), dtype=np.uint8) * 128
    for _ in range(5):
        gate.regions(gray)

    # Add a bright blob in the middle
    moving = gray.copy()
    moving[200:280, 280:360] = 255  # 80x80 white blob

    regions = gate.regions(moving)
    assert len(regions) >= 1
    # Each region is (x1, y1, x2, y2)
    r = regions[0]
    assert len(r) == 4
    # Blob should overlap the expected area
    x1, y1, x2, y2 = r
    assert x1 < 360 and x2 > 280
    assert y1 < 280 and y2 > 200


def test_small_regions_filtered():
    """Tiny motion (below min_region_area) should be filtered out."""
    from pipeline.motion_gate import MotionGate
    gate = MotionGate(min_region_area=1000)
    gray = np.ones((480, 640, 3), dtype=np.uint8) * 128
    for _ in range(5):
        gate.regions(gray)
    # Tiny 5x5 blob = 25 px² — below threshold
    moving = gray.copy()
    moving[100:105, 100:105] = 255
    regions = gate.regions(moving)
    assert regions == []
