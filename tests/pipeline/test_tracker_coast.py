"""S1: a track coasts through detection gaps for ~hit_counter_max frames.

The Pi's COCO detector misses a perched bird for stretches; a longer coast
keeps the track (and its ID) alive across those gaps instead of dying at ~3s.
This verifies the coast scales with hit_counter_max so the tuning lever works.
"""
from types import SimpleNamespace

from pipeline.tracker import BirdTracker


def _det(box, conf=0.9):
    # Tracker only reads .box and .confidence.
    return SimpleNamespace(box=box, confidence=conf)


def _coast_frames(hit_max):
    t = BirdTracker(distance_threshold=2.5, hit_counter_max=hit_max,
                    initialization_delay=2)
    box = (100, 100, 140, 140)
    # Saturate hit_counter toward hit_max with steady detections.
    for i in range(hit_max + 6):
        t.update([_det(box)], float(i * 33))
    # Starve: count frames the track stays active with no detections.
    coast = 0
    for i in range(hit_max + 30):
        out = t.update([], float((hit_max + 6 + i) * 33))
        if not out.active:
            break
        coast += 1
    return coast


def test_coast_scales_with_hit_counter_max():
    low = _coast_frames(5)
    high = _coast_frames(20)
    assert low >= 3, f"expected some coast at max=5, got {low}"
    assert high > low, f"higher hit_counter_max must coast longer: {high} !> {low}"


def test_coasting_flag_reflects_detection_hits():
    """Track.coasting: False on frames where the track matched a detection,
    True on frames where it survives only via Kalman coast (bbox is frozen at
    the last detection then — the overlay renders these differently)."""
    t = BirdTracker(distance_threshold=2.5, hit_counter_max=10,
                    initialization_delay=2)
    box = (100, 100, 140, 140)
    out = None
    for i in range(6):
        out = t.update([_det(box)], float(i * 33))
    assert out.active, "track should be active after steady detections"
    assert out.active[0].coasting is False, "hit this frame -> not coasting"

    out = t.update([], 6 * 33.0)
    assert out.active, "track should coast through a one-frame gap"
    assert out.active[0].coasting is True, "no detection this frame -> coasting"

    out = t.update([_det(box)], 7 * 33.0)
    assert out.active
    assert out.active[0].coasting is False, "re-detection clears coasting"
