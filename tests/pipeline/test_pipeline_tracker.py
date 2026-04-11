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


def test_track_frame_count_increments_per_hit():
    """Track.frame_count must increment only when that specific track gets a hit.

    BirdTracker uses initialization_delay=1, so a detection only appears in active[]
    after 2 consecutive hits (the first frame is Norfair's initializing period).
    frame_count counts hits from the first frame a track enters our active dict
    (post-initialization), so it is always >= 1 when a track is first seen in
    active[] and increments on every subsequent detection hit.
    """
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    tracker = BirdTracker()

    # Frame 1: initializing — not yet active, frame_count not yet tracked.
    tracker.update([Detection(box=[100, 100, 200, 200], confidence=0.9)], frame_time_ms=1000)

    # Frame 2: exits initializing state → appears in active[] with frame_count=1.
    out2 = tracker.update([Detection(box=[100, 100, 200, 200], confidence=0.9)], frame_time_ms=1100)
    assert len(out2.active) == 1, f"expected 1 active track after priming, got {len(out2.active)}"
    assert out2.active[0].frame_count == 1, (
        f"expected frame_count=1 on first active appearance, got {out2.active[0].frame_count}"
    )

    # Frame 3: same bird (slight movement) → frame_count=2
    out3 = tracker.update([Detection(box=[105, 100, 205, 200], confidence=0.9)], frame_time_ms=1200)
    assert len(out3.active) == 1
    assert out3.active[0].frame_count == 2, (
        f"expected frame_count=2, got {out3.active[0].frame_count}"
    )

    # Frames 4-5: second bird appears far away while old bird continues.
    # Frame 4: new bird initializing, old bird gets another hit (frame_count=3).
    old_track_id = out2.active[0].track_id
    out4 = tracker.update([
        Detection(box=[110, 100, 210, 200], confidence=0.9),  # old bird
        Detection(box=[500, 500, 600, 600], confidence=0.8),  # new bird (initializing)
    ], frame_time_ms=1300)
    assert len(out4.active) == 1, f"expected 1 (new bird still initializing), got {len(out4.active)}"
    assert out4.active[0].track_id == old_track_id
    assert out4.active[0].frame_count == 3, f"old track frame 4: expected 3, got {out4.active[0].frame_count}"

    # Frame 5: new bird exits initializing → both active.
    out5 = tracker.update([
        Detection(box=[112, 100, 212, 200], confidence=0.9),  # old bird
        Detection(box=[502, 500, 602, 600], confidence=0.8),  # new bird (now active)
    ], frame_time_ms=1400)

    assert len(out5.active) == 2, f"expected 2 active tracks, got {len(out5.active)}"
    old_track = next(t for t in out5.active if t.track_id == old_track_id)
    new_track = next(t for t in out5.active if t.track_id != old_track_id)
    # Old track: 4 detection hits while active (frames 2-5)
    assert old_track.frame_count == 4, f"old track should be 4, got {old_track.frame_count}"
    # New track: 1 detection hit while active (frame 5)
    assert new_track.frame_count == 1, f"new track should be 1, got {new_track.frame_count}"
