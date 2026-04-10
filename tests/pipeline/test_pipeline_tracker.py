"""Tests for Norfair-based BirdTracker."""
import pytest


def test_new_detection_creates_track():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    det = Detection(box=[100, 100, 200, 200], confidence=0.9)
    out = t.update([det], frame_time_ms=1000)
    # initialization_delay=1 means first hit creates a track on next update
    out2 = t.update([det], frame_time_ms=1050)
    assert len(out2.active) >= 1


def test_moving_detection_stays_same_track():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    # Bird moves slightly between frames
    for i, x in enumerate([100, 105, 110, 115, 120]):
        det = Detection(box=[x, 100, x+100, 200], confidence=0.9)
        out = t.update([det], frame_time_ms=1000 + i*200)
    # After 5 updates, there should be exactly 1 active track
    assert len(out.active) == 1


def test_stationary_detection_flagged_after_10_frames():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    # 12 identical detections — bird hasn't moved
    for i in range(12):
        det = Detection(box=[100, 100, 200, 200], confidence=0.9)
        out = t.update([det], frame_time_ms=1000 + i*200)

    assert len(out.active) == 1
    assert out.active[0].is_stationary is True


def test_tracker_output_dataclass_shape():
    from pipeline.tracker import BirdTracker, TrackerOutput
    t = BirdTracker()
    out = t.update([], frame_time_ms=1000)
    assert isinstance(out, TrackerOutput)
    assert isinstance(out.active, list)
    assert isinstance(out.new, list)
    assert isinstance(out.expired, list)
    assert out.frame_time_ms == 1000


def test_stationary_regions_returns_only_stationary():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    # Create one stationary bird (12 identical detections)
    for i in range(12):
        t.update([Detection(box=[100, 100, 200, 200], confidence=0.9)],
                 frame_time_ms=1000 + i*200)
    regions = t.stationary_regions()
    assert len(regions) == 1
    assert regions[0] == (100, 100, 200, 200)
