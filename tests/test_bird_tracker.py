"""Tests for BirdTracker — IoU-based multi-bird tracking."""
import time
import pytest


class TestIoU:
    def test_perfect_overlap(self):
        from bird_tracker import _iou
        assert _iou([0,0,100,100], [0,0,100,100]) == 1.0

    def test_no_overlap(self):
        from bird_tracker import _iou
        assert _iou([0,0,50,50], [100,100,200,200]) == 0.0

    def test_partial_overlap(self):
        from bird_tracker import _iou
        iou = _iou([0,0,100,100], [50,50,150,150])
        assert 0.1 < iou < 0.2  # ~14% overlap


class TestBirdTracker:
    def test_new_detection_creates_track(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        detections = [{"box": [100,100,200,200], "confidence": 0.9}]
        species = ["Song Sparrow"]
        tracks = tracker.update(detections, species)
        assert len(tracks) == 1
        assert tracks[0]["species"] == "Song Sparrow"
        assert tracks[0]["is_new"] is True

    def test_same_position_reuses_track(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        det = [{"box": [100,100,200,200], "confidence": 0.9}]
        sp = ["Song Sparrow"]
        t1 = tracker.update(det, sp)
        t2 = tracker.update(det, sp)
        assert t1[0]["track_id"] == t2[0]["track_id"]
        assert t2[0]["is_new"] is False

    def test_different_position_creates_new_track(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        tracker.update([{"box": [0,0,50,50], "confidence": 0.9}], ["Sparrow"])
        tracks = tracker.update([{"box": [500,500,600,600], "confidence": 0.9}], ["Cardinal"])
        # Should have 2 tracks (old hasn't expired yet)
        assert len(tracks) >= 1
        assert any(t["species"] == "Cardinal" for t in tracks)

    def test_multiple_birds(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        dets = [
            {"box": [0,0,100,100], "confidence": 0.9},
            {"box": [300,300,400,400], "confidence": 0.8},
        ]
        tracks = tracker.update(dets, ["Sparrow", "Cardinal"])
        assert len(tracks) == 2
        species = {t["species"] for t in tracks}
        assert species == {"Sparrow", "Cardinal"}

    def test_track_expires_after_timeout(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker(expire_seconds=0.1)
        tracker.update([{"box": [100,100,200,200], "confidence": 0.9}], ["Sparrow"])
        time.sleep(0.2)
        expired = tracker.get_expired_tracks()
        assert len(expired) == 1
        assert expired[0]["species"] == "Sparrow"

    def test_keeper_frame_is_highest_confidence(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        tracker.update([{"box": [100,100,200,200], "confidence": 0.7}], ["Sparrow"],
                       frame_data=b"low_conf_frame")
        tracker.update([{"box": [100,100,200,200], "confidence": 0.95}], ["Sparrow"],
                       frame_data=b"high_conf_frame")
        tracker.update([{"box": [100,100,200,200], "confidence": 0.8}], ["Sparrow"],
                       frame_data=b"medium_conf_frame")
        # The keeper should be the high confidence frame
        tracks = tracker.get_active_tracks()
        assert tracks[0]["keeper_data"] == b"high_conf_frame"

    def test_max_tracks_evicts_oldest(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker(max_tracks=3)
        for i in range(5):
            tracker.update(
                [{"box": [i*100, 0, i*100+50, 50], "confidence": 0.9}],
                [f"Bird{i}"]
            )
        assert len(tracker.get_active_tracks()) <= 3

    def test_max_lifetime(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker(max_lifetime=0.1)
        tracker.update([{"box": [100,100,200,200], "confidence": 0.9}], ["Sparrow"])
        time.sleep(0.15)
        # Even with matching detection, track should expire
        expired = tracker.get_expired_tracks()
        assert len(expired) == 1

    def test_session_id_exists(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        assert tracker.session_id is not None
        assert len(tracker.session_id) > 0
