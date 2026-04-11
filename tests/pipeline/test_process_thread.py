"""Tests for CameraProcessThread — the per-camera orchestrator."""
import queue
import time
import numpy as np
from unittest.mock import MagicMock


def test_process_thread_reads_frame_and_calls_pipeline():
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame

    frame_q = queue.Queue(maxsize=2)

    motion_gate = MagicMock()
    motion_gate.regions = MagicMock(return_value=[(0, 0, 100, 100)])

    detector = MagicMock()
    from pipeline.detector import Detection
    detector.detect = MagicMock(return_value=[
        Detection(box=[10, 10, 50, 50], confidence=0.9)
    ])

    from pipeline.tracker import Track, TrackerOutput
    track = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
                  bbox=[10, 10, 50, 50], confidence=0.9)
    tracker = MagicMock()
    tracker.update = MagicMock(return_value=TrackerOutput(
        active=[track], new=[track], expired=[], frame_time_ms=0
    ))
    tracker.stationary_regions = MagicMock(return_value=[])
    tracker.tracks = {1: track}

    classifier = MagicMock()
    from pipeline.classifier import ClassificationResult
    classifier.classify = MagicMock(return_value=ClassificationResult(
        species="Test Bird", confidence=0.9, model_source="yard", should_retry=False
    ))
    classifier.stats = {"yard": 0}

    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder",
        frame_queue=frame_q,
        motion_gate=motion_gate,
        detector=detector,
        tracker=tracker,
        classifier=classifier,
        event_store=event_store,
        annotator=annotator,
        health=health,
    )
    thread.start()

    frame = Frame(
        bgr=np.ones((480, 640, 3), dtype=np.uint8) * 128,
        wall_time_ms=1000,
        camera="feeder",
        width=640,
        height=480,
    )
    frame_q.put(frame)

    # Give the thread a moment to process
    time.sleep(0.3)

    # Verify pipeline was called
    motion_gate.regions.assert_called()
    detector.detect.assert_called()
    tracker.update.assert_called()
    classifier.classify.assert_called()
    event_store.write_event.assert_called()
    annotator.submit.assert_called()

    thread.stop()


def test_process_thread_survives_detector_exception():
    """An exception in the detector should not crash the thread."""
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame

    frame_q = queue.Queue(maxsize=2)
    motion_gate = MagicMock()
    motion_gate.regions.return_value = [(0,0,10,10)]
    detector = MagicMock()
    detector.detect.side_effect = RuntimeError("boom")
    tracker = MagicMock()
    from pipeline.tracker import TrackerOutput
    tracker.update.return_value = TrackerOutput(active=[], new=[], expired=[], frame_time_ms=0)
    tracker.stationary_regions.return_value = []
    tracker.tracks = {}
    classifier = MagicMock()
    classifier.stats = {}
    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder", frame_queue=frame_q, motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=classifier,
        event_store=event_store, annotator=annotator, health=health,
    )
    thread.start()

    frame = Frame(bgr=np.zeros((10,10,3), dtype=np.uint8),
                  wall_time_ms=1000, camera="feeder", width=10, height=10)
    frame_q.put(frame)
    time.sleep(0.3)

    # Thread should still be alive
    assert thread.is_alive()
    thread.stop()


def test_process_thread_retries_track_when_coral_busy():
    """If classifier returns should_retry=True, track stays needs_classification=True."""
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    from pipeline.tracker import Track, TrackerOutput
    from pipeline.detector import Detection
    from pipeline.classifier import ClassificationResult

    frame_q = queue.Queue(maxsize=2)
    motion_gate = MagicMock()
    motion_gate.regions.return_value = [(0,0,100,100)]
    detector = MagicMock()
    detector.detect.return_value = [Detection(box=[10,10,50,50], confidence=0.9)]

    # One track that stays "needs_classification" across updates
    track = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
                  bbox=[10,10,50,50], confidence=0.9)
    tracker = MagicMock()
    tracker.update.return_value = TrackerOutput(
        active=[track], new=[track], expired=[], frame_time_ms=0
    )
    tracker.stationary_regions.return_value = []
    tracker.tracks = {1: track}

    # Classifier returns should_retry=True (Coral busy)
    classifier = MagicMock()
    classifier.classify.return_value = ClassificationResult(
        species=None, confidence=0, model_source=None, should_retry=True
    )
    classifier.stats = {}

    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder", frame_queue=frame_q, motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=classifier,
        event_store=event_store, annotator=annotator, health=health,
    )
    thread.start()

    frame = Frame(bgr=np.ones((480,640,3), dtype=np.uint8)*128,
                  wall_time_ms=1000, camera="feeder", width=640, height=480)
    frame_q.put(frame)
    time.sleep(0.3)

    # Track should still be flagged for classification (retry next frame)
    assert track.needs_classification is True
    # Attempts counter incremented
    assert track.classification_attempts == 1
    thread.stop()


def test_yolo_p99_uses_np_percentile_and_returns_none_for_few_samples():
    """p99 must be computed via np.percentile (not sorted slice hack) AND
    must return None when fewer than 10 samples are available."""
    import numpy as np
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()

    # Case A: only 3 samples → p99 must be None
    t._stats = {
        "frames_processed": 3, "detections": 0,
        "yolo_ms_samples": [50.0, 60.0, 70.0],
    }
    t.tracker = MagicMock()
    t.tracker.tracks = []
    t.tracker.stationary_regions.return_value = []
    t.classifier = MagicMock()
    t.classifier.stats = {}
    t.health = MagicMock()
    captured = {}
    def capture(camera, section, payload):
        captured[section] = payload
    t.health.update = capture

    frame = Frame(bgr=np.zeros((360, 640, 3), dtype=np.uint8),
                  wall_time_ms=time.time() * 1000,
                  camera="test", width=640, height=360)
    t._update_health(frame, det_ms=50.0)

    assert captured["detector"]["yolo_ms_p99"] is None, (
        f"with 3 samples p99 must be None, got {captured['detector']['yolo_ms_p99']}"
    )

    # Case B: 100 samples with a clean distribution → np.percentile(samples, 99) exact
    samples = [10.0] * 50 + [20.0] * 40 + [1000.0] * 10  # top 10% = 1000
    t._stats["yolo_ms_samples"] = list(samples)
    captured.clear()
    t._update_health(frame, det_ms=10.0)
    expected = round(float(np.percentile(samples, 99)))
    assert captured["detector"]["yolo_ms_p99"] == expected, (
        f"yolo_ms_p99={captured['detector']['yolo_ms_p99']}, expected {expected}"
    )


def test_yolo_samples_excludes_skip_frames():
    """When motion_regions is empty and not forced_full, YOLO is skipped and
    that zero-cost timing must NOT be recorded in yolo_ms_samples."""
    import queue, threading, time
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()
    t._stats = {
        "frames_processed": 0,
        "detections": 0,
        "yolo_ms_samples": [],
        "yolo_runs_total": 0,
        "yolo_skipped_motion": 0,
    }
    t._last_forced_full = time.time()  # not due for forced full

    motion_gate = MagicMock()
    motion_gate.regions.return_value = []  # no motion

    detector = MagicMock()
    detector.detect.return_value = []  # empty fast-path

    tracker_out = MagicMock()
    tracker_out.new = []
    tracker_out.active = []
    tracker_out.expired = []
    tracker = MagicMock()
    tracker.update.return_value = tracker_out
    tracker.tracks = []
    tracker.stationary_regions.return_value = []

    classifier = MagicMock(); classifier.stats = {}
    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.annotator = annotator
    t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="test", width=640, height=360,
    )

    for _ in range(5):
        t._process_frame(frame)

    assert t._stats["yolo_ms_samples"] == [], (
        f"Expected empty samples, got {t._stats['yolo_ms_samples']}"
    )
    assert t._stats["yolo_skipped_motion"] == 5
    assert t._stats["yolo_runs_total"] == 0


def test_write_track_summary_uses_per_track_frame_count():
    """write_track_summary must pass track.frame_count, not a global counter."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()
    t._stats = {
        "frames_processed": 9999,  # deliberately big, not per-track
        "detections": 0,
        "yolo_ms_samples": [],
        "yolo_runs_total": 0,
        "yolo_skipped_motion": 0,
    }
    t._last_forced_full = time.time()

    fake_track = MagicMock()
    fake_track.frame_count = 42

    tracker_out = MagicMock()
    tracker_out.new = []
    tracker_out.active = []
    tracker_out.expired = [fake_track]

    motion_gate = MagicMock(); motion_gate.regions.return_value = []
    detector = MagicMock(); detector.detect.return_value = []
    tracker = MagicMock(); tracker.update.return_value = tracker_out
    tracker.tracks = []; tracker.stationary_regions.return_value = []
    classifier = MagicMock(); classifier.stats = {}
    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.annotator = annotator
    t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="test", width=640, height=360,
    )
    t._process_frame(frame)

    event_store.write_track_summary.assert_called_once()
    call_kwargs = event_store.write_track_summary.call_args.kwargs
    assert call_kwargs["num_frames"] == 42, (
        f"expected num_frames=42 (per-track), got {call_kwargs['num_frames']}"
    )


def test_classifier_stats_reported_per_camera_not_global():
    """process_thread must pull only its own camera's slice of classifier stats."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {
        "frames_processed": 1, "detections": 0,
        "yolo_ms_samples": [10.0] * 15,  # ≥10 so p99 isn't None
        "yolo_runs_total": 15, "yolo_skipped_motion": 0,
    }

    classifier = MagicMock()
    classifier.stats = {
        "feeder": {"yard": 42, "aiy": 3, "unlabeled_call": 1,
                   "both_agree": 0, "lock_timeouts": 0, "retries": 0},
        "ground": {"yard": 0, "aiy": 100, "unlabeled_call": 5,
                   "both_agree": 0, "lock_timeouts": 0, "retries": 0},
    }
    t.classifier = classifier

    t.tracker = MagicMock()
    t.tracker.tracks = []
    t.tracker.stationary_regions.return_value = []

    captured = {}
    health = MagicMock()
    def fake_update(camera, section, payload):
        captured[(camera, section)] = payload
    health.update = fake_update
    t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="feeder", width=640, height=360,
    )
    t._update_health(frame, det_ms=0.0)

    feeder_classifier_stats = captured[("feeder", "classifier")]
    assert feeder_classifier_stats["yard"] == 42
    assert feeder_classifier_stats["aiy"] == 3
    # Ground's aiy=100 must NOT leak into feeder's stats
    assert feeder_classifier_stats["aiy"] != 100
    # And ground's stats must NOT appear anywhere in the feeder update
    assert "ground" not in feeder_classifier_stats
