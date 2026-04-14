"""Tests for BirdDetector — motion gate, stationary suppression, full-frame detection."""
import numpy as np
import pytest
from unittest.mock import MagicMock, patch


def test_no_motion_skips_yolo_entirely():
    """When motion_regions is empty AND forced_full is False, skip YOLO.

    Region-based detection was abandoned because ONNX resizes inputs to
    640x640 anyway — multiple regions = multiple full-cost YOLO calls.
    The motion gate alone now decides whether to run detection.
    """
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
    yolo_mock.detect.assert_not_called()
    assert detections == []


def test_motion_present_runs_full_frame():
    """When motion_regions is non-empty, run YOLO ONCE on the full frame."""
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

    # Even with multiple motion regions, YOLO should be called exactly ONCE
    detections = d.detect(
        frame,
        [(100, 100, 200, 200), (300, 300, 400, 400), (500, 500, 600, 600)],
        forced_full=False,
    )
    yolo_mock.detect.assert_called_once()
    assert len(detections) == 1


def test_forced_full_runs_on_whole_frame():
    """When forced_full=True, YOLO runs on the full frame even with no motion."""
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
