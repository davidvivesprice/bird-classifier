"""Tests for BirdDetector — region detection + coordinate offset."""
import numpy as np
import pytest
from unittest.mock import MagicMock, patch


def test_detection_coordinates_are_full_frame():
    """YOLO runs on a crop, but returned boxes are in full-frame coordinates."""
    from pipeline.detector import BirdDetector, Detection

    # Construct without calling __init__ (we want to inject mocks)
    d = BirdDetector.__new__(BirdDetector)

    # Mock YOLODetector.detect to return a box in CROP-LOCAL coordinates
    yolo_mock = MagicMock()
    yolo_mock.detect = MagicMock(return_value=[
        {"box": [10, 20, 60, 80], "confidence": 0.9}  # crop-local
    ])
    d.yolo = yolo_mock
    d.get_stationary = lambda: []

    frame_bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    # Motion region offset by (400, 200)
    region = (400, 200, 500, 300)
    detections = d._detect_region(frame_bgr, region)

    assert len(detections) == 1
    # Box should be offset: crop(10,20,60,80) → full(410,220,460,280)
    assert detections[0].box == [410, 220, 460, 280]
    assert detections[0].confidence == pytest.approx(0.9)


def test_stationary_only_region_is_skipped():
    """A motion region that contains ONLY stationary tracks should be skipped."""
    from pipeline.detector import BirdDetector
    from pipeline.frame import Frame

    d = BirdDetector.__new__(BirdDetector)
    yolo_mock = MagicMock()
    yolo_mock.detect = MagicMock(return_value=[])
    d.yolo = yolo_mock
    # One stationary track covering (400,200,500,300)
    d.get_stationary = lambda: [(400, 200, 500, 300)]

    frame_bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame = Frame(bgr=frame_bgr, wall_time_ms=0, camera="test", width=1920, height=1080)
    motion_regions = [(400, 200, 500, 300)]  # identical to stationary

    detections = d.detect(frame, motion_regions, forced_full=False)
    # YOLO should NOT have been called
    yolo_mock.detect.assert_not_called()
    assert detections == []


def test_forced_full_runs_on_whole_frame():
    """When forced_full=True, YOLO runs on the full frame ignoring motion regions."""
    from pipeline.detector import BirdDetector
    from pipeline.frame import Frame

    d = BirdDetector.__new__(BirdDetector)
    yolo_mock = MagicMock()
    yolo_mock.detect = MagicMock(return_value=[
        {"box": [100, 100, 200, 200], "confidence": 0.8}
    ])
    d.yolo = yolo_mock
    d.get_stationary = lambda: []

    frame_bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame = Frame(bgr=frame_bgr, wall_time_ms=0, camera="test", width=1920, height=1080)

    detections = d.detect(frame, [], forced_full=True)

    yolo_mock.detect.assert_called_once()
    assert len(detections) == 1
    assert detections[0].box == [100, 100, 200, 200]


def test_no_motion_regions_falls_through_to_full_frame():
    """If no motion regions (but not forced), fall through to full-frame detection.
    (This is the initial warmup case before motion is detected)."""
    from pipeline.detector import BirdDetector
    from pipeline.frame import Frame

    d = BirdDetector.__new__(BirdDetector)
    yolo_mock = MagicMock()
    yolo_mock.detect = MagicMock(return_value=[])
    d.yolo = yolo_mock
    d.get_stationary = lambda: []

    frame_bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    frame = Frame(bgr=frame_bgr, wall_time_ms=0, camera="test", width=1920, height=1080)

    detections = d.detect(frame, [], forced_full=False)
    # With no motion regions, spec says fall through to full-frame
    yolo_mock.detect.assert_called_once()
