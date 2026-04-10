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
