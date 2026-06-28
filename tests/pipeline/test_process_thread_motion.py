"""F1: the process thread skips MOG2 when the detector runs full-frame."""
from collections import deque
from unittest.mock import MagicMock

import numpy as np

from pipeline.process_thread import CameraProcessThread


def _make_thread(detector):
    """Build a CameraProcessThread without __init__ (documented test pattern)."""
    pt = CameraProcessThread.__new__(CameraProcessThread)
    pt.name = "feeder"
    pt.motion_gate = MagicMock()
    pt.motion_gate.regions.return_value = []
    pt.detector = detector
    tracker_out = MagicMock(active=[], new=[])
    pt.tracker = MagicMock()
    pt.tracker.update.return_value = tracker_out
    pt.classifier = MagicMock()
    pt.event_store = MagicMock()
    pt.snapshot_writer = None
    pt.sse_server = MagicMock()
    pt.health = MagicMock()
    pt.capture = MagicMock()
    pt.capture.restarts_last_hour.return_value = 0
    pt.disagreement_detector = MagicMock()
    pt._dry_run = True
    pt._last_forced_full = 0.0
    pt._last_debug_encode_ms = 0
    pt._last_health_update_ms = 0
    pt._stats = {
        "frames_processed": 0,
        "detections": 0,
        "yolo_ms_samples": deque(maxlen=100),
        "yolo_runs_total": 0,
        "yolo_skipped_motion": 0,
    }
    return pt


def _frame():
    f = MagicMock()
    f.bgr = np.zeros((360, 640, 3), dtype=np.uint8)
    f.wall_time_ms = 0.0
    f.width = 640
    f.height = 360
    return f


def test_motion_skipped_when_detector_does_not_use_it():
    det = MagicMock()
    det.uses_motion_regions = False
    det.detect.return_value = []
    pt = _make_thread(det)
    pt._process_frame(_frame())
    det.detect.assert_called_once()             # YOLO still runs (full-frame)
    pt.motion_gate.regions.assert_not_called()  # MOG2 NOT computed
    # full-frame detector counts as "ran" every frame
    assert pt._stats["yolo_runs_total"] == 1
    assert pt._stats["yolo_skipped_motion"] == 0


def test_motion_computed_when_detector_uses_it():
    det = MagicMock()
    det.uses_motion_regions = True
    det.detect.return_value = []
    pt = _make_thread(det)
    pt._process_frame(_frame())
    pt.motion_gate.regions.assert_called_once()
